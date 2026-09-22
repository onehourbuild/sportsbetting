"""Polymarket US market data. Keyless, read-only, and deliberately unadventurous.

polymarket.com and polymarket.us are different exchanges. .com blocks US residents from
trading; .us is the regulated US product, and a US owner's funded account lives there.
The two price the same game almost identically, but they differ in ways that matter:

* **Fee.** .us charges a taker coefficient of 0.0695 against .com's 0.05, published
  per market as ``feeCoefficient``.
* **Identity.** .us markets are keyed by ``marketSlug``
  (``asc-nfl-nyg-lar-2026-09-21-pos-6pt5``); there is no ``conditionId`` and no CLOB
  token id, so :attr:`PmOutcome.token_id` is synthesised from the slug and the outcome
  index. It is stable and unique, which is all the ledger needs of it.
* **Labels that contradict each other.** This is the dangerous one. The market titled
  "Los Angeles Rams wins by over 6.5 points" carries the question "Will the New York
  Giants cover 6.5…" and outcomes ``["-6.50", "+6.50"]``. Title, question and outcomes
  disagree about whose side is whose.

That last point drives the whole design. Getting a side backwards does not produce a
smaller edge or a missing row: it recommends the opposite team, at a price that looks
right, with a stake sized confidently. It is the most expensive bug available here and it
would be invisible until settlement.

So this client resolves a side only where the payload says so unambiguously, and refuses
everything else with a reason that surfaces on the Diagnostics page. A market this client
declines is a market nobody can lose money on. Spreads are refused wholesale today, for
exactly the labelling reason above; extending that needs a live payload to anchor against,
not a guess (see ``docs/RESEARCH.md``, "Verified live 2026-09-21").

Nothing here places, cancels or sizes an order, and nothing here holds a key: the market
data endpoints are public. The authenticated fills API is a separate concern.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from app.clients.polymarket import (
    TeamResolver,
    _as_bool,
    _as_float,
    _default_resolver,
    _mirror,
    _Skip,
    parse_iso_utc,
)
from app.clients.transport import Transport
from app.core.types import LEAGUES, League, MarketType, PmMarket, PmOutcome

log = logging.getLogger(__name__)

GATEWAY = "https://gateway.polymarket.us"

# Page size for /v1/events. Large enough that a full slate is one or two requests.
PAGE_LIMIT = 100
# Stop rather than page forever if the API keeps returning full pages.
MAX_PAGES = 40

# `sportsMarketType` looks like "football_team_full_game_spread". Only full-game markets
# map onto what this app prices; halves, quarters and player props are a different model.
FULL_GAME_SUFFIXES: dict[str, MarketType] = {
    "full_game_winner": "moneyline",
    "full_game_spread": "spread",
    "full_game_total": "total",
}

# The sport each league's markets should carry, used to corroborate the slug.
LEAGUE_SPORT: dict[str, str] = {"nfl": "football", "nba": "basketball", "mlb": "baseball"}

OVER_LABELS = frozenset({"over", "o", "yes"})
UNDER_LABELS = frozenset({"under", "u", "no"})

SKIP_NOT_FULL_GAME = "not a full-game market"
SKIP_UNKNOWN_TYPE = "unrecognised sportsMarketType"
SKIP_SPREAD_AMBIGUOUS = (
    "spread sides are ambiguous on .us (title, question and outcomes disagree); "
    "refused rather than risk pricing the wrong team"
)
SKIP_NO_TEAMS = "could not resolve both outcomes to this event's two teams"
SKIP_NO_OVER_UNDER = "total outcomes are not recognisably Over/Under"
SKIP_NO_LINE = "no line on a market that needs one"
SKIP_NO_PRICES = "no usable price on either outcome"


class PolymarketUsError(Exception):
    """A .us request failed or returned something unusable."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        # Some fields arrive as a JSON-encoded string rather than a list.
        text = value.strip()
        if text.startswith("["):
            import json

            try:
                parsed = json.loads(text)
            except ValueError:
                return []
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        return []
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    return []


def _quote_for(raw: Mapping[str, Any], key: str, index: int) -> float | None:
    """`bestAskQuote` may be a per-outcome list or a single number for outcome 0.

    A scalar is mirrored for outcome 1, which is what a two-outcome binary market means:
    the ask on one side is 1 minus the bid on the other. Returning None is always safe --
    the caller treats a missing price as "cannot price this".
    """
    value = raw.get(key)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        if index < len(value):
            return _as_float(value[index])
        return None
    price = _as_float(value)
    if price is None:
        return None
    return price if index == 0 else _mirror(price)


def _market_type_of(raw: Mapping[str, Any]) -> MarketType:
    sports_type = str(raw.get("sportsMarketType") or "").strip().lower()
    if not sports_type:
        raise _Skip(SKIP_UNKNOWN_TYPE)
    for suffix, market_type in FULL_GAME_SUFFIXES.items():
        if sports_type.endswith(suffix):
            return market_type
    # A recognisable sport with an unrecognised market is a half, quarter or prop.
    raise _Skip(SKIP_NOT_FULL_GAME)


class PolymarketUsClient:
    """Reads public .us market data and emits the same `PmMarket` the scan already eats.

    Emitting the existing contract rather than a new one is the point: matching, the edge
    math, sizing, the ledger and the forward test all work unchanged, and the venue
    becomes a data-source choice rather than a second pipeline to keep in step.
    """

    def __init__(
        self,
        transport: Transport,
        gateway_base: str = GATEWAY,
        team_resolver: TeamResolver | None = None,
    ) -> None:
        self.transport = transport
        self.gateway_base = gateway_base.rstrip("/")
        self._team_resolver = team_resolver
        # Per-market `feeCoefficient` values seen by the last events() call, as
        # {market_slug, rate}. Diagnostics shows these: a fee that silently drifted from
        # 0.0695 changes every edge on the page, so it is worth being able to see it.
        self.fee_coefficients: list[dict] = []
        # Markets refused for a reason worth surfacing rather than burying.
        self.duplicates_dropped: list[str] = []

    # -- public API ----------------------------------------------------------

    def events(
        self, league: League, *, include_closed: bool = False
    ) -> tuple[list[PmMarket], list[dict]]:
        """Full-game markets for `league`; second item = what was refused and why."""
        if league not in LEAGUES:
            raise PolymarketUsError(f"unknown league {league!r}; expected one of {LEAGUES}")
        markets: list[PmMarket] = []
        unparseable: list[dict] = []
        seen: set[str] = set()
        self.fee_coefficients = []
        self.duplicates_dropped = []

        for event in self._iter_events(league):
            if not include_closed and _as_bool(event.get("closed")):
                continue
            home_key, away_key = self._event_teams(event, league)
            for raw in event.get("markets") or []:
                if not isinstance(raw, dict):
                    continue
                slug = str(raw.get("marketSlug") or raw.get("slug") or "")
                if slug and slug in seen:
                    log.warning(".us %s: duplicate market %s dropped", league, slug)
                    self.duplicates_dropped.append(f"market:{slug}")
                    continue
                try:
                    market = self._parse_market(raw, event, league, home_key, away_key)
                except _Skip as skip:
                    unparseable.append(
                        {
                            "market_id": slug,
                            "question": str(raw.get("question") or raw.get("title") or ""),
                            "reason": skip.reason,
                        }
                    )
                    continue
                seen.add(market.market_id)
                markets.append(market)
        return markets, unparseable

    # -- fetching ------------------------------------------------------------

    def _iter_events(self, league: League) -> Iterator[dict]:
        """Page /v1/events and yield the ones belonging to `league`.

        The endpoint takes no league filter, so events are selected by slug prefix
        (`nfl-...`) and corroborated by the sport in `sportsMarketType`. Filtering client
        side is slower than a query parameter and is the only option the API offers.
        """
        offset = 0
        for _ in range(MAX_PAGES):
            payload = self._get(
                f"{self.gateway_base}/v1/events",
                {"limit": PAGE_LIMIT, "offset": offset},
                "events",
            )
            page = payload.get("events") if isinstance(payload, dict) else payload
            if not isinstance(page, list):
                raise PolymarketUsError(f".us events: expected a list, got {type(page).__name__}")
            if not page:
                return
            for event in page:
                if isinstance(event, dict) and self._event_is(event, league):
                    yield event
            if len(page) < PAGE_LIMIT:
                return
            offset += PAGE_LIMIT

    def _event_is(self, event: Mapping[str, Any], league: League) -> bool:
        slug = str(event.get("slug") or "").strip().lower()
        if slug.startswith(f"{league}-"):
            return True
        # Fall back to the sport carried on the event's markets, which distinguishes the
        # three leagues this app knows about even when a slug is shaped differently.
        sport = LEAGUE_SPORT[league]
        for raw in event.get("markets") or []:
            if isinstance(raw, dict):
                sports_type = str(raw.get("sportsMarketType") or "").lower()
                if sports_type.startswith(f"{sport}_"):
                    # Only trust this when the slug does not claim another league.
                    return not any(slug.startswith(f"{other}-") for other in LEAGUES)
        return False

    def _get(self, url: str, params: Mapping[str, Any], what: str) -> Any:
        try:
            payload, _ = self.transport.get_json(url, params=params)
        except Exception as exc:  # transport raises its own error type
            raise PolymarketUsError(f".us {what} fetch failed: {exc}") from exc
        return payload

    # -- parsing -------------------------------------------------------------

    def _resolve(self, name: str, league: League) -> str | None:
        resolver = self._team_resolver or _default_resolver
        return resolver(name, league)

    def _event_teams(
        self, event: Mapping[str, Any], league: League
    ) -> tuple[str | None, str | None]:
        """(home, away). .us event slugs read `<league>-<away>-<home>-<date>`."""
        home = self._resolve(str(event.get("homeTeam") or ""), league)
        away = self._resolve(str(event.get("awayTeam") or ""), league)
        if home and away:
            return home, away
        parts = str(event.get("slug") or "").split("-")
        if len(parts) >= 3:
            away = away or self._resolve(parts[1], league)
            home = home or self._resolve(parts[2], league)
        return home, away

    def _parse_market(
        self,
        raw: Mapping[str, Any],
        event: Mapping[str, Any],
        league: League,
        home_key: str | None,
        away_key: str | None,
    ) -> PmMarket:
        market_type = _market_type_of(raw)
        if market_type == "spread":
            # Deliberate. See this module's docstring: on .us the title, the question and
            # the outcomes disagree about which team each side belongs to, and a wrong
            # answer recommends the opposite team at a plausible price. Refusing costs
            # some coverage; guessing costs money.
            raise _Skip(SKIP_SPREAD_AMBIGUOUS)

        slug = str(raw.get("marketSlug") or raw.get("slug") or "")
        if not slug:
            raise _Skip("market has no slug to key it by")
        labels = _as_str_list(raw.get("outcomes"))
        if len(labels) != 2:
            raise _Skip(f"expected 2 outcomes, got {len(labels)}")
        prices = _as_str_list(raw.get("outcomePrices"))

        if market_type == "moneyline":
            keys = self._moneyline_keys(labels, league, home_key, away_key)
            line: float | None = None
            line_team_key: str | None = None
        else:
            keys = self._total_keys(labels)
            line = _as_float(raw.get("line")) or _as_float(raw.get("points"))
            if line is None:
                raise _Skip(SKIP_NO_LINE)
            line_team_key = None

        outcomes = tuple(
            PmOutcome(
                token_id=f"{slug}#{index}",
                name=label,
                team_key=keys[index],
                last_price=_as_float(prices[index]) if index < len(prices) else None,
                best_bid=_quote_for(raw, "bestBidQuote", index),
                best_ask=_quote_for(raw, "bestAskQuote", index),
            )
            for index, label in enumerate(labels)
        )
        if not any(o.best_ask is not None or o.last_price is not None for o in outcomes):
            raise _Skip(SKIP_NO_PRICES)

        fee = _as_float(raw.get("feeCoefficient"))
        if fee is not None:
            self.fee_coefficients.append({"market_slug": slug, "rate": fee})

        status = str(raw.get("status") or "").strip().lower()
        closed = _as_bool(raw.get("closed")) or status in {"closed", "resolved", "settled"}

        return PmMarket(
            market_id=slug,
            # .us has no conditionId; the slug is the identity, so it plays both roles.
            condition_id=slug,
            slug=slug,
            question=str(raw.get("question") or raw.get("title") or ""),
            event_id=str(event.get("id") or event.get("slug") or ""),
            event_slug=str(event.get("slug") or ""),
            event_title=str(event.get("title") or event.get("name") or ""),
            league=league,
            market_type=market_type,
            line=line,
            line_team_key=line_team_key,
            outcomes=outcomes,  # type: ignore[arg-type]
            game_start=parse_iso_utc(raw.get("gameStartTime") or event.get("gameStartTime")),
            home_team_key=home_key,
            away_team_key=away_key,
            accepting_orders=not closed and status not in {"paused", "halted"},
            closed=closed,
            resolved_outcome_index=None,
            tick_size=_as_float(raw.get("orderPriceMinTickSize")),
            min_order_size=_as_float(raw.get("minimumTradeQty")),
            liquidity=_as_float(raw.get("liquidity")),
            volume=_as_float(raw.get("volume") or raw.get("volumeFp")),
            taker_fee_rate=fee,
        )

    def _moneyline_keys(
        self,
        labels: Sequence[str],
        league: League,
        home_key: str | None,
        away_key: str | None,
    ) -> tuple[str | None, str | None]:
        """Both outcomes must resolve to this event's two teams, or the market is refused.

        Accepting a partial match would put a priced row under a team the event does not
        contain, which is how the .com client once wrote lines under the wrong game.
        """
        resolved = tuple(self._resolve(label, league) for label in labels)
        if None in resolved or resolved[0] == resolved[1]:
            raise _Skip(SKIP_NO_TEAMS)
        if home_key and away_key and set(resolved) != {home_key, away_key}:
            raise _Skip(SKIP_NO_TEAMS)
        return resolved  # type: ignore[return-value]

    def _total_keys(self, labels: Sequence[str]) -> tuple[None, None]:
        """Totals carry no team, but the Over/Under labels still have to be recognisable."""
        lowered = [label.strip().lower() for label in labels]
        has_over = any(any(word in label for word in OVER_LABELS) for label in lowered)
        has_under = any(any(word in label for word in UNDER_LABELS) for label in lowered)
        if not (has_over and has_under):
            raise _Skip(SKIP_NO_OVER_UNDER)
        return (None, None)


__all__ = [
    "GATEWAY",
    "SKIP_NOT_FULL_GAME",
    "SKIP_NO_OVER_UNDER",
    "SKIP_NO_PRICES",
    "SKIP_NO_TEAMS",
    "SKIP_SPREAD_AMBIGUOUS",
    "PolymarketUsClient",
    "PolymarketUsError",
]

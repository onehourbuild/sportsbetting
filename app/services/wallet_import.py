"""Import the owner's Polymarket fills into the bet ledger. Read-only, keyless.

Why this exists. The ledger could only be written from an edge card, so a bet the owner
placed by hand on Polymarket (or one the app never flagged) was invisible to settlement
and closing line value, which are the only things the project is judged on. Polymarket
publishes every fill on a public feed filterable by wallet, so the ledger can be filled
from the exchange's own record instead of from memory.

What it does NOT do: place, cancel or size an order, or touch a key. The wallet address
is public information; the feed is the same one anyone can read for any address.

Rules, each of which has a test:
- Only BUY fills become bets. A SELL is an exit; v1 does not model exits and reports them
  as skipped so they are visible rather than silently dropped.
- Fills in one transaction on one outcome are one bet (an order that walked the book is
  still one decision): shares are summed, the price is the volume-weighted average.
- The bet is keyed by ``<transactionHash>:<asset>``; a re-import is a no-op.
- Only markets the app already tracks (by ``conditionId``) are imported. Anything else is
  reported as "unknown market".
- Fills at or after kickoff are skipped. A pre-game closing line says nothing about an
  in-play price, so the CLV would be fiction.
- The fee is the taker fee at the preference rate: the feed does not say whether the fill
  was maker or taker, and assuming taker is the conservative reading.
- ``fair_at_bet`` is the app's most recent fair value for that outcome at or before the
  fill (a forward sample, else an opportunity), so an imported bet gets the same
  edge-at-bet an app-logged one would.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.polymarket import PolymarketClient
from app.core.edge import taker_fee_per_share
from app.models import DEFAULT_PREFS, Bet, ForwardSample, Game, Market, Opportunity

log = logging.getLogger(__name__)

SOURCE_WALLET = "wallet"
# Fills older than this are not looked at: the app only tracks markets it has scanned.
LOOKBACK = timedelta(days=45)
# Skipped entries kept in the scan notes (the counts are always complete).
MAX_SKIPPED_NOTES = 40

SKIP_SELL = "sell (exits are not modelled)"
SKIP_UNKNOWN_MARKET = "unknown market (not tracked by the app)"
SKIP_NOT_AN_OUTCOME = "asset is not an outcome of the market"
SKIP_AFTER_KICKOFF = "after kickoff"
SKIP_UNPARSEABLE = "unparseable"


@dataclass(frozen=True)
class Fill:
    tx: str
    asset: str
    condition_id: str
    side: str
    price: float
    size: float
    when: datetime
    title: str


@dataclass
class ImportResult:
    wallet: str
    fetched: int = 0
    imported: int = 0
    already: int = 0
    skipped_counts: Counter[str] = field(default_factory=Counter)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    bet_ids: list[int] = field(default_factory=list)

    @property
    def n_skipped(self) -> int:
        return sum(self.skipped_counts.values())

    def as_note(self) -> dict[str, Any]:
        """The blob Diagnostics shows under "Other notes"."""
        return {
            "wallet": self.wallet,
            "fetched": self.fetched,
            "imported": self.imported,
            "already": self.already,
            "skipped_counts": dict(self.skipped_counts),
            "skipped": list(self.skipped),
        }

    def summary(self) -> str:
        short = f"{self.wallet[:6]}...{self.wallet[-4:]}" if len(self.wallet) > 12 else self.wallet
        return (
            f"wallet {short}: fetched={self.fetched} imported={self.imported} "
            f"already={self.already} skipped={self.n_skipped}"
        )


# --------------------------------------------------------------------------- parsing


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_fill(raw: Mapping[str, Any]) -> Fill | None:
    """One data-api trade row -> Fill, or None when a required field is missing or bad."""
    tx = str(raw.get("transactionHash") or "").strip().lower()
    asset = str(raw.get("asset") or "").strip()
    condition_id = str(raw.get("conditionId") or "").strip().lower()
    side = str(raw.get("side") or "").strip().upper()
    price = _as_float(raw.get("price"))
    size = _as_float(raw.get("size"))
    ts = _as_float(raw.get("timestamp"))
    if not (tx and asset and condition_id) or side not in ("BUY", "SELL"):
        return None
    if price is None or size is None or ts is None:
        return None
    if not 0.0 < price < 1.0 or size <= 0.0:
        return None
    try:
        when = datetime.fromtimestamp(int(ts), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return Fill(
        tx=tx,
        asset=asset,
        condition_id=condition_id,
        side=side,
        price=price,
        size=size,
        when=when,
        title=str(raw.get("title") or ""),
    )


def group_fills(fills: list[Fill]) -> list[list[Fill]]:
    """Fills sharing (transaction, asset, side) are one order; oldest group first so bet
    ids follow the order the bets were placed."""
    groups: dict[tuple[str, str, str], list[Fill]] = {}
    for fill in fills:
        groups.setdefault((fill.tx, fill.asset, fill.side), []).append(fill)
    return sorted(groups.values(), key=lambda g: (min(f.when for f in g), g[0].tx))


def import_key(fill: Fill) -> str:
    return f"{fill.tx}:{fill.asset}"


# --------------------------------------------------------------------------- fair value


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fair_at(session: Session, token: str, when: datetime) -> float | None:
    """The app's latest fair probability for `token` at or before `when`: the most recent
    forward sample, else the most recent opportunity. None when it never priced it."""
    sample = session.scalars(
        select(ForwardSample)
        .where(ForwardSample.token == token, ForwardSample.sampled_at <= when)
        .order_by(ForwardSample.sampled_at.desc(), ForwardSample.id.desc())
    ).first()
    if sample is not None and sample.fair_prob is not None:
        return float(sample.fair_prob)
    opp = session.scalars(
        select(Opportunity)
        .where(Opportunity.token == token, Opportunity.computed_at <= when)
        .order_by(Opportunity.computed_at.desc(), Opportunity.id.desc())
    ).first()
    if opp is not None and opp.fair_prob is not None:
        return float(opp.fair_prob)
    return None


# --------------------------------------------------------------------------- import


def _note_skip(result: ImportResult, reason: str, fill: Fill | None, raw: Any = None) -> None:
    result.skipped_counts[reason] += 1
    if len(result.skipped) >= MAX_SKIPPED_NOTES:
        return
    entry: dict[str, Any] = {"reason": reason}
    if fill is not None:
        entry.update(
            {
                "tx": fill.tx[:14],
                "title": fill.title,
                "side": fill.side,
                "price": fill.price,
                "size": fill.size,
                "at": fill.when.isoformat(),
            }
        )
    elif isinstance(raw, Mapping):
        entry["tx"] = str(raw.get("transactionHash") or "")[:14]
        entry["title"] = str(raw.get("title") or "")
    result.skipped.append(entry)


def import_fills(
    session: Session,
    fills_raw: list[Mapping[str, Any]],
    *,
    wallet: str,
    prefs: Any,
) -> ImportResult:
    """Turn raw trade rows into Bet rows. No network; commits once at the end."""
    result = ImportResult(wallet=wallet, fetched=len(fills_raw))
    fee_rate = float(getattr(prefs, "taker_fee_rate", None) or DEFAULT_PREFS["taker_fee_rate"])

    fills: list[Fill] = []
    for raw in fills_raw:
        fill = parse_fill(raw) if isinstance(raw, Mapping) else None
        if fill is None:
            _note_skip(result, SKIP_UNPARSEABLE, None, raw)
            continue
        fills.append(fill)

    known_keys = {
        key for (key,) in session.execute(select(Bet.import_key).where(Bet.import_key.is_not(None)))
    }
    new_bets: list[Bet] = []
    for group in group_fills(fills):
        first = group[0]
        if first.side != "BUY":
            _note_skip(result, SKIP_SELL, first)
            continue
        key = import_key(first)
        if key in known_keys:
            result.already += 1
            continue
        market = session.scalars(
            select(Market).where(Market.condition_id == first.condition_id)
        ).first()
        if market is None:
            _note_skip(result, SKIP_UNKNOWN_MARKET, first)
            continue
        if first.asset == market.outcome_a_token:
            outcome_name, outcome_key = market.outcome_a_name, market.outcome_a_key
        elif first.asset == market.outcome_b_token:
            outcome_name, outcome_key = market.outcome_b_name, market.outcome_b_key
        else:
            _note_skip(result, SKIP_NOT_AN_OUTCOME, first)
            continue
        placed_at = min(f.when for f in group)
        game = session.get(Game, market.game_id) if market.game_id else None
        kickoff = _as_utc(game.start_time) if game is not None else None
        if kickoff is not None and placed_at >= kickoff:
            _note_skip(result, SKIP_AFTER_KICKOFF, first)
            continue

        shares = sum(f.size for f in group)
        cost = sum(f.price * f.size for f in group)
        price = cost / shares
        fee_per_share = taker_fee_per_share(price, fee_rate)
        fee_usd = shares * fee_per_share
        fair = fair_at(session, first.asset, placed_at)
        bet = Bet(
            market_id=market.id,
            token=first.asset,
            outcome_key=outcome_key,
            outcome_name=outcome_name,
            mode="taker",
            price=round(price, 6),
            shares=round(shares, 6),
            stake_usd=round(cost + fee_usd, 2),
            fee_usd=round(fee_usd, 4),
            fair_at_bet=fair,
            edge_at_bet=(fair - (price + fee_per_share)) if fair is not None else None,
            placed_at=placed_at,
            status="open",
            notes=f"{len(group)} fill(s), tx {first.tx[:10]}",
            source=SOURCE_WALLET,
            import_key=key,
        )
        session.add(bet)
        new_bets.append(bet)
        known_keys.add(key)
        result.imported += 1

    if new_bets:
        session.commit()
        result.bet_ids = [b.id for b in new_bets]
    return result


def import_wallet_trades(
    session: Session,
    polymarket: PolymarketClient,
    *,
    wallet: str,
    now: datetime,
    prefs: Any,
) -> ImportResult:
    """Fetch the wallet's fills (last `LOOKBACK`) and import them. See the module docstring."""
    wallet = (wallet or "").strip().lower()
    if not wallet:
        raise ValueError("wallet must not be empty")
    now = _as_utc(now) or datetime.now(UTC)
    rows = polymarket.wallet_trades(wallet, since=now - LOOKBACK)
    result = import_fills(session, rows, wallet=wallet, prefs=prefs)
    log.info("wallet import: %s", result.summary())
    return result

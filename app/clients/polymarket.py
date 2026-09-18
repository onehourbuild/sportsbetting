"""Polymarket Gamma (events/markets) + CLOB (order books) client. Contract: docs/ARCHITECTURE.md.

STUB — replaced by the Polymarket client implementer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.clients.transport import Transport
from app.core.types import League, OrderBook, PmMarket

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

TeamResolver = Callable[[str, League], "str | None"]  # defaults to matching.team_key, lazily


class PolymarketClient:
    def __init__(
        self,
        transport: Transport,
        gamma_base: str = GAMMA,
        clob_base: str = CLOB,
        team_resolver: TeamResolver | None = None,
    ) -> None:
        self.transport = transport
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self._team_resolver = team_resolver

    def events(
        self, league: League, *, include_closed: bool = False
    ) -> tuple[list[PmMarket], list[dict]]:
        """Paginate /events?tag_slug=<league>; second item = unparseable markets
        [{market_id, question, reason}]."""
        raise NotImplementedError

    def market(self, market_id: str) -> PmMarket | None:
        """GET /markets/{id} (used for settlement)."""
        raise NotImplementedError

    def order_books(self, token_ids: Sequence[str]) -> dict[str, OrderBook]:
        """POST /books in chunks of 200; token_id -> OrderBook."""
        raise NotImplementedError

    def teams(self, league: League) -> list[dict]:
        """GET /teams?league=<league>."""
        raise NotImplementedError

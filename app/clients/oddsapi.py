"""The Odds API v4 client. Contract: docs/ARCHITECTURE.md.

STUB — replaced by the books client implementer.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.clients.polymarket import TeamResolver
from app.clients.transport import Transport
from app.core.types import BookGame, League, QuotaInfo

SPORT_KEYS = {"nfl": "americanfootball_nfl", "nba": "basketball_nba", "mlb": "baseball_mlb"}


class OddsApiClient:
    def __init__(
        self,
        transport: Transport,
        api_key: str,
        base: str = "https://api.the-odds-api.com/v4",
        team_resolver: TeamResolver | None = None,
    ) -> None:
        self.transport = transport
        self.api_key = api_key
        self.base = base.rstrip("/")
        self._team_resolver = team_resolver

    def odds(
        self,
        league: League,
        bookmakers: Sequence[str],
        markets: Sequence[str] = ("h2h", "spreads", "totals"),
    ) -> tuple[list[BookGame], QuotaInfo]:
        """GET /sports/{sport_key}/odds with bookmakers=...; returns games and quota headers."""
        raise NotImplementedError

    @staticmethod
    def estimate_cost(n_markets: int, n_bookmakers: int) -> int:
        """n_markets * ceil(n_bookmakers / 10) credits."""
        raise NotImplementedError

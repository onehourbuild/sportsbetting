"""ESPN scoreboard client (keyless fallback, single low-weight book).

Contract: docs/ARCHITECTURE.md.

STUB — replaced by the books client implementer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.clients.polymarket import TeamResolver
from app.clients.transport import Transport
from app.core.types import BookGame, EspnGame, League


class EspnClient:
    def __init__(
        self,
        transport: Transport,
        base: str = "https://site.api.espn.com/apis/site/v2/sports",
        team_resolver: TeamResolver | None = None,
    ) -> None:
        self.transport = transport
        self.base = base.rstrip("/")
        self._team_resolver = team_resolver

    def scoreboard(self, league: League, date: date | None = None) -> list[EspnGame]:
        """GET .../{sport}/{league}/scoreboard?dates=YYYYMMDD."""
        raise NotImplementedError

    @staticmethod
    def to_book_games(games: Sequence[EspnGame]) -> list[BookGame]:
        """bookmaker "espn"; h2h + spreads + totals when present."""
        raise NotImplementedError

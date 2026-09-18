"""Demo mode: seed the DB from synthetic fixtures. STUB — implemented by the integration step."""

from __future__ import annotations

from sqlalchemy.orm import Session


def seed_demo(session: Session) -> None:
    """Run a fixture-backed scan (all three leagues) and log one demo bet on the closed
    BAL@TOR market so settlement and CLV have something to show."""
    raise NotImplementedError

"""`python -m app.cli` prints one-line summaries and exits non-zero on failure."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import cli
from app.models import Bet, Scan
from app.settings import Settings, set_settings


@pytest.fixture
def demo_cli(settings: Settings, engine, db_session: Session):
    """Point the CLI at the test engine and a demo-mode Settings."""
    demo = settings.model_copy(update={"demo_mode": True})
    set_settings(demo)
    try:
        yield db_session
    finally:
        set_settings(None)


def test_demo_seed_scan_and_settle(demo_cli: Session, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["demo-seed"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "demo data ready: scans=1 opportunities=4 bets=2"

    assert cli.main(["scan", "--kind", "poly", "--league", "nfl"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("scan #2 kind=poly leagues=nfl markets=6 matched=6 opps=1")
    assert line.endswith("credits_used=None remaining=None errors=0")

    assert cli.main(["scan", "--kind", "books"]) == 0
    line = capsys.readouterr().out.strip()
    assert "kind=books leagues=nfl,nba,mlb markets=17 matched=17 opps=4" in line
    assert "credits_used=9 remaining=497 errors=0" in line

    assert cli.main(["settle"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("settled 0 bet(s) via scan #4 kind=poly")
    assert demo_cli.scalar(select(func.count()).select_from(Scan)) == 4
    assert demo_cli.scalar(select(func.count()).select_from(Bet)) == 2


def test_cli_reports_failures_with_exit_code(
    demo_cli: Session, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("gamma timeout")

    monkeypatch.setattr("app.services.scan.run_scan_default", boom)
    assert cli.main(["scan"]) == 2
    assert "scan failed: RuntimeError: gamma timeout" in capsys.readouterr().err


def test_parser_choices():
    parser = cli.build_parser()
    args = parser.parse_args(["scan", "--kind", "both", "--league", "nfl", "--league", "mlb"])
    assert args.kind == "both" and args.league == ["nfl", "mlb"]
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "--kind", "magic"])


# --------------------------------------------------------------------------- review fixes


def test_demo_seed_never_touches_the_network_and_is_refused_outside_dev(
    settings: Settings, engine, db_session: Session, monkeypatch: pytest.MonkeyPatch, capsys
):
    """demo-seed with DEMO_MODE off must still be fixture-only, idempotent on the demo bets,
    and refused for a production configuration."""

    class NoNetwork:
        def __init__(self, *args, **kwargs):
            raise AssertionError("demo-seed must never build an HTTP transport")

    monkeypatch.setattr("app.services.scan.HttpTransport", NoNetwork)
    monkeypatch.setattr("app.clients.transport.HttpTransport", NoNetwork)
    assert settings.demo_mode is False and settings.app_env == "dev"
    set_settings(settings)
    try:
        assert cli.main(["demo-seed"]) == 0
        assert capsys.readouterr().out.strip() == "demo data ready: scans=1 opportunities=4 bets=2"
        assert cli.main(["demo-seed"]) == 0  # idempotent on the demo bets, not on "any scan"
        assert "scans=1 opportunities=4 bets=2" in capsys.readouterr().out

        prod = settings.model_copy(
            update={
                "app_env": "prod",
                "app_password": "correct-horse-battery",
                "secret_key": "s" * 64,
            }
        )
        set_settings(prod)
        assert cli.main(["demo-seed"]) == 2
        assert "demo-seed refused" in capsys.readouterr().err
    finally:
        set_settings(None)


def test_demo_clear_removes_the_slate_and_refuses_while_real_bets_exist(
    demo_cli: Session, capsys: pytest.CaptureFixture[str]
):
    from app.models import Game, Opportunity
    from app.services.bets import create_bet
    from app.services.prefs import get_prefs

    assert cli.main(["demo-seed"]) == 0
    capsys.readouterr()
    opp = demo_cli.scalars(select(Opportunity).order_by(Opportunity.id)).first()
    mine = create_bet(demo_cli, opp.id, 5.0, opp.ask, "taker", notes="mine")
    assert cli.main(["demo-clear"]) == 2
    assert "refusing to clear: 1 real bet" in capsys.readouterr().err
    demo_cli.expire_all()
    assert demo_cli.scalar(select(func.count()).select_from(Bet)) == 3  # nothing removed

    demo_cli.delete(demo_cli.get(Bet, mine.id))
    demo_cli.commit()
    assert cli.main(["demo-clear"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("demo data cleared:") and "bets=2" in out and "scans=1" in out
    demo_cli.expire_all()
    for model in (Bet, Scan, Game, Opportunity):
        assert demo_cli.scalar(select(func.count()).select_from(model)) == 0, model
    assert get_prefs(demo_cli).bankroll == 1000.0  # preferences survive
    assert cli.main(["demo-clear"]) == 0  # nothing left: still fine


def test_demo_seed_refuses_a_ledger_with_real_bets(demo_cli: Session, caplog):
    import logging

    from app.models import Market
    from app.services.demo import seed_demo

    demo_cli.add(Market(id="500701", market_type="moneyline"))
    demo_cli.add(
        Bet(
            market_id="500701",
            token="1" * 70,
            outcome_name="Orioles",
            mode="taker",
            price=0.42,
            shares=23.0,
            stake_usd=10.0,
            fee_usd=0.0,
            status="open",
            notes="mine",
        )
    )
    demo_cli.commit()
    with caplog.at_level(logging.WARNING, logger="app.services.demo"):
        seed_demo(demo_cli)
    assert demo_cli.scalar(select(func.count()).select_from(Scan)) == 0
    assert any("demo seed skipped: 1 real bet" in r.getMessage() for r in caplog.records)

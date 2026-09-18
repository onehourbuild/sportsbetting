"""`python -m app.cli scan|settle|demo-seed`."""

from __future__ import annotations

import argparse
import sys

from app.db import get_engine, get_session_factory, init_db
from app.logsetup import configure_logging
from app.settings import get_settings


def _session():
    settings = get_settings()
    engine = get_engine(settings.database_url)
    init_db(engine)
    return get_session_factory()()


def _summary(result) -> str:  # type: ignore[no-untyped-def]
    return (
        f"scan #{result.scan_id} kind={result.kind} leagues={','.join(result.leagues)} "
        f"markets={result.n_markets} matched={result.n_matched} opps={result.n_opps} "
        f"credits_used={result.credits_used} remaining={result.credits_remaining} "
        f"errors={len(result.errors)}"
    )


def cmd_scan(args: argparse.Namespace) -> int:
    from app.services.scan import run_scan_default

    leagues = args.league or None
    with _session() as session:
        # `now` stays None so demo mode uses its fixed clock and live mode the wall clock.
        result = run_scan_default(session, kind=args.kind, leagues=leagues)
    print(_summary(result))
    for err in result.errors:
        print(f"  error: {err}")
    return 0 if not result.errors else 1


def cmd_settle(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from app.models import Bet
    from app.services.scan import run_scan_default

    with _session() as session:
        open_before = session.scalar(
            select(func.count()).select_from(Bet).where(Bet.status == "open")
        )
        result = run_scan_default(session, kind="poly")
        open_after = session.scalar(
            select(func.count()).select_from(Bet).where(Bet.status == "open")
        )
    settled = int(open_before or 0) - int(open_after or 0)
    print(f"settled {settled} bet(s) via {_summary(result)}")
    for err in result.errors:
        print(f"  error: {err}")
    return 0 if not result.errors else 1


def cmd_demo_seed(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from app.models import Bet, Opportunity, Scan
    from app.services.demo import seed_demo

    settings = get_settings()
    if settings.app_env != "dev" and not settings.demo_mode:
        # Synthetic bets must never land in a live ledger by accident.
        print(
            "demo-seed refused: APP_ENV is not 'dev' and DEMO_MODE is off. Seed only a "
            "development database (APP_ENV=dev) or run with DEMO_MODE=true.",
            file=sys.stderr,
        )
        return 2
    with _session() as session:
        seed_demo(session, settings)
        n_scans = session.scalar(select(func.count()).select_from(Scan))
        n_opps = session.scalar(select(func.count()).select_from(Opportunity))
        n_bets = session.scalar(select(func.count()).select_from(Bet))
    print(f"demo data ready: scans={n_scans} opportunities={n_opps} bets={n_bets}")
    return 0


def cmd_demo_clear(args: argparse.Namespace) -> int:
    from app.services.demo import clear_demo

    with _session() as session:
        removed = clear_demo(session)  # raises ValueError when real bets are present
    summary = " ".join(f"{table}={n}" for table, n in removed.items())
    print(f"demo data cleared: {summary}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Polymarket Edge Finder CLI")
    parser.add_argument("--log-level", default=None, help="override LOG_LEVEL")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="run a scan and print the summary")
    scan.add_argument("--kind", choices=("poly", "books", "both"), default="poly")
    scan.add_argument("--league", action="append", choices=("nfl", "nba", "mlb"))
    scan.set_defaults(func=cmd_scan)

    settle = sub.add_parser("settle", help="refresh Polymarket and settle open bets")
    settle.set_defaults(func=cmd_settle)

    demo = sub.add_parser("demo-seed", help="seed synthetic fixture data (dev / DEMO_MODE only)")
    demo.set_defaults(func=cmd_demo_seed)

    clear = sub.add_parser(
        "demo-clear",
        help="delete the synthetic demo bets and slate (refuses when real bets exist)",
    )
    clear.set_defaults(func=cmd_demo_clear)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level or get_settings().log_level)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - a CLI prints the failure and exits non-zero
        print(f"{args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

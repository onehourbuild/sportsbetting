"""`python -m app.cli scan|settle|demo-seed`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from app.db import get_engine, get_session_factory, init_db
from app.settings import get_settings


def _session():
    settings = get_settings()
    engine = get_engine(settings.database_url)
    init_db(engine)
    return get_session_factory()()


def cmd_scan(args: argparse.Namespace) -> int:
    from app.services.scan import run_scan_default

    leagues = args.league or None
    with _session() as session:
        result = run_scan_default(session, kind=args.kind, leagues=leagues, now=datetime.now(UTC))
    print(
        f"scan #{result.scan_id} kind={result.kind} leagues={','.join(result.leagues)} "
        f"markets={result.n_markets} matched={result.n_matched} opps={result.n_opps} "
        f"credits_used={result.credits_used} remaining={result.credits_remaining}"
    )
    for err in result.errors:
        print(f"  error: {err}")
    return 0 if not result.errors else 1


def cmd_settle(args: argparse.Namespace) -> int:
    from app.services.scan import run_scan_default

    with _session() as session:
        result = run_scan_default(session, kind="poly", now=datetime.now(UTC))
    print(f"settle via scan #{result.scan_id}: errors={len(result.errors)}")
    return 0


def cmd_demo_seed(args: argparse.Namespace) -> int:
    from app.services.demo import seed_demo

    with _session() as session:
        seed_demo(session)
    print("demo data seeded")
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

    demo = sub.add_parser("demo-seed", help="seed synthetic fixture data")
    demo.set_defaults(func=cmd_demo_seed)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=(args.log_level or get_settings().log_level))
    try:
        return int(args.func(args))
    except NotImplementedError as exc:
        print(f"not implemented yet: {exc or args.command}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

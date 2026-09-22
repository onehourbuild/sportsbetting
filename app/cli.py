"""`python -m app.cli scan|settle|import-wallet|demo-seed|demo-clear|forward-report`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def cmd_import_wallet(args: argparse.Namespace) -> int:
    """Import the owner's Polymarket fills into the ledger (read-only; see wallet_import)."""
    from datetime import UTC, datetime

    from app.clients.polymarket import PolymarketClient
    from app.clients.transport import HttpTransport
    from app.services.prefs import get_prefs
    from app.services.wallet_import import import_wallet_trades

    settings = get_settings()
    with _session() as session:
        prefs = get_prefs(session)
        wallet = (args.wallet or prefs.pm_wallet or "").strip()
        if not wallet:
            print("no wallet: set it in Settings or pass --wallet 0x...", file=sys.stderr)
            return 2
        if settings.demo_mode:
            from app.services.demo import DEMO_NOW, build_demo_transport

            transport = build_demo_transport(settings)
            now = DEMO_NOW
        else:
            transport = HttpTransport()
            now = datetime.now(UTC)
        try:
            result = import_wallet_trades(
                session, PolymarketClient(transport), wallet=wallet, now=now, prefs=prefs
            )
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
    print(result.summary())
    for reason, n in sorted(result.skipped_counts.items()):
        print(f"  skipped {n}: {reason}")
    return 0


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


def cmd_forward_report(args: argparse.Namespace) -> int:
    from app.services import forward

    thresholds = (
        tuple(sorted({float(t) / 100.0 for t in args.threshold}))
        if args.threshold
        else forward.DEFAULT_THRESHOLDS
    )
    with _session() as session:
        data = forward.report(session, thresholds=thresholds, league=args.league)
    print(forward.format_report(data))
    return 0


def cmd_backtest_harvest(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    from app.clients.transport import HttpTransport
    from app.services import backtest

    transport = HttpTransport()

    def get_json(url: str, params: dict) -> object:
        payload, _headers = transport.get_json(url, params)
        return payload

    since = datetime.now(UTC) - timedelta(days=args.days) if args.days else None
    leagues = args.league or list(backtest.LEAGUES)
    print(
        f"harvesting {', '.join(leagues)} "
        f"({'all history' if since is None else f'last {args.days} days'})...",
        flush=True,
    )

    def progress(stats: backtest.HarvestStats) -> None:
        print(f"  ... {stats.stored} stored, {stats.resolved} resolved markets seen", flush=True)

    with _session() as session:
        stats = backtest.harvest(
            session,
            get_json,
            leagues=leagues,
            max_pages=args.pages,
            since=since,
            limit=args.limit,
            on_progress=progress,
        )
        session.commit()
    transport.close()
    print(
        f"harvested: {stats.stored} outcomes stored from {stats.resolved} resolved markets "
        f"({stats.skipped_existing} already had, {stats.skipped_no_trades} had no usable trades)"
    )
    return 0


def cmd_backtest_report(args: argparse.Namespace) -> int:
    from app.services import backtest

    with _session() as session:
        data = backtest.report(
            session,
            league=args.league,
            market_type=args.market_type,
            fee_rate=args.fee,
            paired=args.paired,
            max_close_age_hours=(
                backtest.DEFAULT_MAX_CLOSE_AGE_HOURS if args.max_age is None else args.max_age
            ),
        )
    print(backtest.format_report(data))
    return 0


def cmd_strategy_bakeoff(args: argparse.Namespace) -> int:
    from app.services import strategies

    with _session() as session:
        data = strategies.run(
            session,
            league=args.league,
            # argparse cannot tell "not given" from a real value here, so each default is
            # resolved to the module's rather than passed through as None.
            fee_rate=strategies.DEFAULT_FEE_RATE if args.fee is None else args.fee,
            max_close_age_hours=(
                strategies.DEFAULT_MAX_CLOSE_AGE_HOURS if args.max_age is None else args.max_age
            ),
            paired=not args.unpaired,
            reps=strategies.DEFAULT_PERMUTATIONS if args.reps is None else args.reps,
            min_n=strategies.DEFAULT_MIN_N if args.min_n is None else args.min_n,
            seed=args.seed,
        )
    print(strategies.format_run(data, top=args.top))
    if args.csv:
        written = strategies.write_csv(data, Path(args.csv))
        for path in written:
            print(f"wrote {path}")
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

    imp = sub.add_parser(
        "import-wallet", help="import your Polymarket fills into the bet ledger (read-only)"
    )
    imp.add_argument("--wallet", default=None, help="0x address; default: the Settings value")
    imp.set_defaults(func=cmd_import_wallet)

    demo = sub.add_parser("demo-seed", help="seed synthetic fixture data (dev / DEMO_MODE only)")
    demo.set_defaults(func=cmd_demo_seed)

    clear = sub.add_parser(
        "demo-clear",
        help="delete the synthetic demo bets and slate (refuses when real bets exist)",
    )
    clear.set_defaults(func=cmd_demo_clear)

    fwd = sub.add_parser(
        "forward-report",
        help="what each edge threshold would have returned on the recorded samples",
    )
    fwd.add_argument("--league", choices=("nfl", "nba", "mlb"), default=None)
    fwd.add_argument(
        "--threshold",
        action="append",
        type=float,
        metavar="PCT",
        help="edge threshold in percent (repeatable); default 0, 0.5, 1, 2, 3, 5",
    )
    fwd.set_defaults(func=cmd_forward_report)

    harvest = sub.add_parser(
        "backtest-harvest",
        help="pull Polymarket's resolved sports markets and their pre-kickoff prices",
    )
    harvest.add_argument("--league", action="append", choices=("nfl", "nba", "mlb"))
    harvest.add_argument("--days", type=int, default=None, help="only games this recent")
    harvest.add_argument("--pages", type=int, default=60, help="Gamma pages per league")
    harvest.add_argument("--limit", type=int, default=None, help="stop after N outcomes")
    harvest.set_defaults(func=cmd_backtest_harvest)

    back = sub.add_parser(
        "backtest-report", help="calibration and return by price band over harvested history"
    )
    back.add_argument("--league", choices=("nfl", "nba", "mlb"), default=None)
    back.add_argument("--market-type", choices=("moneyline", "spreads", "totals"), default=None)
    back.add_argument("--fee", type=float, default=0.05, help="taker fee coefficient")
    back.add_argument(
        "--max-age",
        type=float,
        default=None,
        help="max hours between the closing trade and kickoff (default 12)",
    )
    back.add_argument(
        "--paired",
        action="store_true",
        help="only markets whose two sides form one simultaneous quote (recommended)",
    )
    back.set_defaults(func=cmd_backtest_report)

    bake = sub.add_parser(
        "strategy-bakeoff",
        help="score many betting rules over the harvested history and test the winner",
    )
    bake.add_argument("--league", choices=("nfl", "nba", "mlb"), default=None)
    bake.add_argument("--fee", type=float, default=None, help="taker fee coefficient")
    bake.add_argument("--max-age", type=float, default=None, help="max close age in hours")
    bake.add_argument(
        "--unpaired",
        action="store_true",
        help="include markets with only one side priced (not recommended: stale closes)",
    )
    bake.add_argument("--reps", type=int, default=None, help="shuffled-result repetitions")
    bake.add_argument("--min-n", type=int, default=None, help="picks a rule needs to be ranked")
    bake.add_argument("--seed", type=int, default=1729)
    bake.add_argument("--top", type=int, default=None, help="only print the best N rules")
    bake.add_argument(
        "--csv",
        default=None,
        metavar="DIR",
        help="also write bakeoff-summary.csv and bakeoff-picks.csv into DIR",
    )
    bake.set_defaults(func=cmd_strategy_bakeoff)
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

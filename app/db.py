"""Engine / session helpers. SQLite in v1 with PostgreSQL-compatible naming. No Alembic in v1."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Generator, Sequence
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)

# The taker fee this app hard-coded before it knew Polymarket was two exchanges. A stored
# rate still exactly equal to this is a default nobody chose, not a decision; see
# `reconcile_venue_fee`.
LEGACY_TAKER_FEE_RATE = 0.05

log = logging.getLogger(__name__)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def sqlite_file_path(url: str) -> Path | None:
    """The file behind a file-backed SQLite URL, or None for other drivers / :memory:."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser()


def create_db_engine(url: str) -> Engine:
    """Engine factory. SQLite: check_same_thread off; the parent directory of the DB file is
    created lazily on first connect, so building an engine has no filesystem side effects."""
    connect_args: dict = {}
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        connect_args["check_same_thread"] = False
    engine = create_engine(url, connect_args=connect_args, future=True)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            """WAL + a real busy timeout, because two processes now write this file.

            The forward test runs `python -m app.cli scan` from a scheduled task while the
            web app is serving the same database. In SQLite's default rollback journal a
            writer blocks readers outright, so a page load during a scan fails with
            "database is locked"; WAL lets readers carry on against the last committed
            state. The 30s timeout covers the scan's write bursts -- the Python default is
            5s, which a 3,600-market scan can exceed.
            """
            cursor = dbapi_connection.cursor()
            try:
                # busy_timeout FIRST: switching to WAL needs a brief exclusive lock, and
                # with the default 5s timeout it fails outright while a scan is mid-write
                # (observed). Setting the timeout first makes the switch wait instead.
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    # Another process holds the file and the mode cannot be changed right
                    # now. The old journal mode still works, so never fail startup over it.
                    log.warning("could not switch SQLite to WAL (database busy); continuing")
            finally:
                cursor.close()

    db_file = sqlite_file_path(url) if is_sqlite else None
    if db_file is not None:

        @event.listens_for(engine, "do_connect")
        def _mkdir_before_connect(dialect, conn_rec, cargs, cparams):  # noqa: ANN001
            db_file.parent.mkdir(parents=True, exist_ok=True)

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    """Process-wide engine, created on first use from `url` or Settings.database_url."""
    global _engine, _session_factory
    if _engine is None:
        if url is None:
            from app.settings import get_settings

            url = get_settings().database_url
        _engine = create_db_engine(url)
        _session_factory = create_session_factory(_engine)
    return _engine


def set_engine(engine: Engine) -> None:
    """Replace the process-wide engine (used by create_app and tests)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = create_session_factory(engine)


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, closed afterwards."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db(engine: Engine) -> None:
    """Create missing tables, then add any column a model has that the table lacks.

    v1 has no Alembic; columns added after a database was first created (for example
    `opportunities.fill_complete`) are appended with `ALTER TABLE ... ADD COLUMN`, which
    both SQLite and PostgreSQL support. Columns are never dropped or retyped here.
    """
    from app import models  # noqa: F401  (register tables on Base.metadata)

    Base.metadata.create_all(engine)
    added = add_missing_columns(engine)
    reconcile_venue_fee(engine, added)


def reconcile_venue_fee(engine: Engine, added_columns: Sequence[str]) -> bool:
    """One-time correction for a database written before the venue preference existed.

    `prefs.venue` showing up in `added_columns` means this row predates the app knowing
    that Polymarket is two exchanges. Its `taker_fee_rate` was therefore never a choice
    between them -- it is the old hard-coded 0.05, which is the .com rate. On a .us account
    that understates the fee by 39% and shows up as edge that is not there, which is the
    failure mode `docs/DECISIONS.md` already records as the expensive one.

    A rate the owner had deliberately changed is left alone: only a value still exactly
    equal to the old default counts as "never set". Returns whether anything changed.
    """
    if "prefs.venue" not in set(added_columns):
        return False

    from app.models import VENUE_TAKER_FEE, Prefs

    changed = False
    with Session(engine) as session:
        for prefs in session.query(Prefs).all():
            expected = VENUE_TAKER_FEE.get(prefs.venue)
            if expected is None or prefs.taker_fee_rate != LEGACY_TAKER_FEE_RATE:
                continue
            if expected == LEGACY_TAKER_FEE_RATE:
                continue
            logger.info(
                "prefs %s: venue %s charges %.4f, not the pre-venue default %.4f; correcting",
                prefs.id,
                prefs.venue,
                expected,
                LEGACY_TAKER_FEE_RATE,
            )
            prefs.taker_fee_rate = expected
            changed = True
        if changed:
            session.commit()
    return changed


def add_missing_columns(engine: Engine) -> list[str]:
    """Append model columns missing from existing tables; returns "table.column" names."""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                spec = column.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {spec}'
                default = column.default.arg if column.default is not None else None
                if not column.nullable and default is not None and not callable(default):
                    if isinstance(default, bool):
                        literal = "TRUE" if default else "FALSE"  # SQLite >= 3.23 and PostgreSQL
                    else:
                        literal = repr(default)
                    ddl += f" NOT NULL DEFAULT {literal}"
                connection.exec_driver_sql(ddl)
                added.append(f"{table.name}.{column.name}")
    return added

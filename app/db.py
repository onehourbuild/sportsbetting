"""Engine / session helpers. SQLite in v1 with PostgreSQL-compatible naming. No Alembic in v1."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

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
    add_missing_columns(engine)


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

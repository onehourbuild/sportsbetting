"""Shared fixtures. Everything runs against a temp SQLite file and fixture transports."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.clients.transport import FixtureTransport, Route
from app.db import create_db_engine, create_session_factory, get_session, init_db, set_engine
from app.main import create_app
from app.settings import Settings

FIXED_NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"
TEST_PASSWORD = "test"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]  never read a developer's .env in tests
        app_env="dev",
        app_password=TEST_PASSWORD,
        secret_key="test",
        demo_mode=False,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        fixtures_dir=FIXTURES_DIR,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    set_engine(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(settings: Settings, engine: Engine) -> FastAPI:
    application = create_app(settings)
    application.state.engine = engine
    set_engine(engine)
    factory = create_session_factory(engine)

    def _override_session() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_session] = _override_session
    return application


@pytest.fixture
def anon_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        response = client.post("/login", data={"password": TEST_PASSWORD})
        assert response.status_code == 200, "login should redirect to / and land on 200"
        assert "session" in client.cookies
        yield client


@pytest.fixture
def fixture_transport(settings: Settings):
    """Factory: `fixture_transport(routes, fixtures_dir=None) -> FixtureTransport`."""

    def _make(
        routes: Sequence[Route] | None = None, fixtures_dir: Path | str | None = None
    ) -> FixtureTransport:
        return FixtureTransport(fixtures_dir or settings.fixtures_dir, routes or [])

    return _make


__all__ = ["FIXED_NOW", "FIXTURES_DIR", "TEST_PASSWORD"]

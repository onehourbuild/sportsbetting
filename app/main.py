"""FastAPI app factory: static, routers, DB init, demo seed, auth middleware."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from app import APP_NAME, APP_VERSION
from app.db import create_db_engine, get_session_factory, init_db, set_engine
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "session"
SESSION_MAX_AGE_S = 30 * 24 * 60 * 60  # 30 days
SESSION_VALUE = "ok"
PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/healthz", "/manifest.webmanifest", "/sw.js"})
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)


# --------------------------------------------------------------------------- auth helpers


def make_signer(settings: Settings) -> TimestampSigner:
    return TimestampSigner(settings.secret_key, salt="edge-finder-session")


def sign_session(settings: Settings) -> str:
    return make_signer(settings).sign(SESSION_VALUE).decode("utf-8")


def session_is_valid(settings: Settings, token: str | None) -> bool:
    if not token:
        return False
    try:
        value = make_signer(settings).unsign(token, max_age=SESSION_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return False
    return value == SESSION_VALUE.encode("utf-8")


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def is_authenticated(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return True
    return session_is_valid(settings, request.cookies.get(SESSION_COOKIE))


# --------------------------------------------------------------------------- app factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            engine = create_db_engine(settings.database_url)
            set_engine(engine)
            app.state.engine = engine
        init_db(engine)
        if settings.demo_mode:
            _seed_demo_safely(app)
        app.state.scheduler = _start_scheduler_safely(settings)
        try:
            yield
        finally:
            _stop_scheduler_safely(app)

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    if settings.database_url:
        engine = create_db_engine(settings.database_url)
        set_engine(engine)
        app.state.engine = engine

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if is_public_path(path) or is_authenticated(request):
            return await call_next(request)
        if request.headers.get("HX-Request") == "true":
            response = Response(status_code=401)
            response.headers["HX-Redirect"] = "/login"
            return response
        next_url = path if path.startswith("/") and not path.startswith("//") else "/"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True, "demo": bool(settings.demo_mode), "version": APP_VERSION}

    from app.routes import auth, bets, diagnostics, edges, games, pwa
    from app.routes import settings as settings_routes

    app.include_router(auth.router)
    app.include_router(pwa.router)
    app.include_router(edges.router)
    app.include_router(games.router)
    app.include_router(bets.router)
    app.include_router(settings_routes.router)
    app.include_router(diagnostics.router)
    return app


def _seed_demo_safely(app: FastAPI) -> None:
    from app.services.demo import seed_demo

    session = get_session_factory()()
    try:
        seed_demo(session)
        log.info("demo data seeded")
    except NotImplementedError:
        log.warning("DEMO_MODE is on but services.demo.seed_demo is not implemented yet")
    except Exception:  # noqa: BLE001 - never block startup on demo seeding
        log.exception("demo seeding failed")
    finally:
        session.close()


def _start_scheduler_safely(settings: Settings):  # type: ignore[no-untyped-def]
    if settings.scheduler_poly_minutes <= 0 and settings.scheduler_books_hours <= 0:
        return None
    try:
        from app.services.scheduler import start_scheduler

        return start_scheduler(settings)
    except NotImplementedError:
        log.warning("scheduler requested but services.scheduler is not implemented yet")
    except Exception:  # noqa: BLE001
        log.exception("scheduler failed to start")
    return None


def _stop_scheduler_safely(app: FastAPI) -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return
    try:
        from app.services.scheduler import stop_scheduler

        stop_scheduler(scheduler)
    except Exception:  # noqa: BLE001
        log.exception("scheduler failed to stop")


app = create_app()

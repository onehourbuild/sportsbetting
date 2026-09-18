"""FastAPI app factory: static, routers, DB init, demo seed, auth / CSRF middleware.

One middleware does three things, in order: reject state-changing requests that a browser
reports as cross-site (`is_cross_site_write`), enforce the password gate, and stamp the
security headers onto every response (`apply_security_headers`).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from app import APP_NAME, APP_VERSION
from app.db import create_db_engine, get_session_factory, init_db, set_engine
from app.logsetup import configure_logging
from app.settings import Settings, get_settings, set_settings

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "session"
SESSION_MAX_AGE_S = 30 * 24 * 60 * 60  # 30 days
SESSION_PAYLOAD_LENGTH = 16
PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/healthz", "/manifest.webmanifest", "/sw.js"})
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)
NO_STORE = "no-store"

# CSRF: every state-changing request must look same-origin. SameSite=Lax on the session
# cookie is not enough on its own (it still allows top-level cross-site form POSTs on some
# browsers), and none of these endpoints is safe to trigger from another site: /scan?kind=books
# spends Odds API credits, /bets writes the ledger, /settings rewrites the prefs.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
CROSS_SITE = "cross-site"  # Sec-Fetch-Site value sent by browsers for another site's request
HSTS = "max-age=31536000; includeSubDomains"


# --------------------------------------------------------------------------- auth helpers


def make_signer(settings: Settings) -> TimestampSigner:
    return TimestampSigner(settings.secret_key_value, salt="edge-finder-session")


def session_payload(settings: Settings) -> str:
    """What the cookie signs: an HMAC of the current password under the secret key, so
    changing APP_PASSWORD (or SECRET_KEY) invalidates every cookie ever issued."""
    digest = hmac.new(
        settings.secret_key_value.encode("utf-8"),
        settings.app_password_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:SESSION_PAYLOAD_LENGTH]


def sign_session(settings: Settings) -> str:
    return make_signer(settings).sign(session_payload(settings)).decode("utf-8")


def session_is_valid(settings: Settings, token: str | None) -> bool:
    if not token:
        return False
    try:
        value = make_signer(settings).unsign(token, max_age=SESSION_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(value, session_payload(settings).encode("utf-8"))


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def is_authenticated(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return True
    return session_is_valid(settings, request.cookies.get(SESSION_COOKIE))


# --------------------------------------------------------------------------- CSRF / headers


def _origin_host(value: str | None) -> str | None:
    """Host[:port] of an Origin/Referer header; None when absent or unusable.

    A literal ``null`` origin (sandboxed iframe, ``data:`` document) is reported as the
    sentinel ``"null"`` so it can never match the request Host and is rejected.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower() == "null":
        return "null"
    parsed = urlsplit(text)
    return parsed.netloc.lower() or None


def is_cross_site_write(request: Request) -> bool:
    """True when a state-changing request did not come from this site.

    Checked in order: ``Sec-Fetch-Site`` (browsers send it on every request), then the
    ``Origin`` header, then ``Referer``. A request with none of the three (curl, the CLI,
    TestClient) is allowed: no browser omits all of them on a cross-site POST.
    """
    if request.method.upper() in SAFE_METHODS:
        return False
    if (request.headers.get("sec-fetch-site") or "").strip().lower() == CROSS_SITE:
        return True
    host = (request.headers.get("host") or "").strip().lower()
    for header in ("origin", "referer"):
        origin_host = _origin_host(request.headers.get(header))
        if origin_host is not None:
            return origin_host != host
    return False


def apply_security_headers(response: Response, settings: Settings) -> Response:
    """Headers every response carries, public pages and static assets included."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if settings.cookie_secure:
        # Not in dev: an HSTS header on http://localhost would pin the whole host to HTTPS.
        # fly.toml's force_https only redirects; the first cleartext request on a hostile
        # network can still be answered with a cloned login form that captures the password.
        response.headers["Strict-Transport-Security"] = HSTS
    return response


# --------------------------------------------------------------------------- app factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    set_settings(settings)  # the services layer reads the same Settings the app was built with
    configure_logging(settings.log_level)
    if not settings.auth_enabled:
        log.warning(
            "AUTH DISABLED: APP_ENV=dev with an empty APP_PASSWORD serves every page without "
            "a login. Set APP_PASSWORD (and APP_ENV=prod) before exposing this app."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            engine = create_db_engine(settings.database_url)
            set_engine(engine)
            app.state.engine = engine
        init_db(engine)
        if settings.demo_mode:
            _seed_demo_safely(settings)
        else:
            _warn_about_demo_rows()
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
    from app.routes.auth import LoginLimiter

    app.state.login_limiter = LoginLimiter()
    if settings.database_url:
        engine = create_db_engine(settings.database_url)
        set_engine(engine)
        app.state.engine = engine

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    async def _dispatch(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        if is_cross_site_write(request):
            # %r on a truncated value: the headers are attacker-controlled text.
            log.warning(
                "blocked cross-site %s %s (origin=%r sec-fetch-site=%r)",
                request.method,
                request.url.path,
                (request.headers.get("origin") or "")[:100],
                (request.headers.get("sec-fetch-site") or "")[:20],
            )
            return PlainTextResponse("cross-site request blocked", status_code=403)
        path = request.url.path
        if is_public_path(path):
            return await call_next(request)
        if is_authenticated(request):
            response = await call_next(request)
            # Authenticated pages and partials must never be kept by the browser's HTTP
            # cache or restored from the back/forward cache after logout (lost phone).
            response.headers["Cache-Control"] = NO_STORE
            response.headers["Pragma"] = "no-cache"
            return response
        if request.headers.get("HX-Request") == "true":
            response = Response(status_code=401)
            response.headers["HX-Redirect"] = "/login"
            return response
        next_url = path if path.startswith("/") and not path.startswith("//") else "/"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        return apply_security_headers(await _dispatch(request, call_next), settings)

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


def _seed_demo_safely(settings: Settings) -> None:
    """Seed synthetic fixture data (idempotent). Never blocks startup."""
    from app.services.demo import seed_demo

    session = get_session_factory()()
    try:
        seed_demo(session, settings)
        log.info("demo data ready")
    except Exception:  # noqa: BLE001 - never block startup on demo seeding
        log.exception("demo seeding failed")
    finally:
        session.close()


def _warn_about_demo_rows() -> None:
    """DEMO_MODE is off but synthetic demo bets are still in the ledger: say so loudly."""
    from app.services.demo import demo_rows_present

    session = get_session_factory()()
    try:
        if demo_rows_present(session):
            log.warning(
                "synthetic demo bets are in the ledger while DEMO_MODE is off; they count "
                "in P&L and CLV. Remove them with: python -m app.cli demo-clear"
            )
    except Exception:  # noqa: BLE001 - a diagnostic must never block startup
        log.exception("demo row check failed")
    finally:
        session.close()


def _start_scheduler_safely(settings: Settings):  # type: ignore[no-untyped-def]
    if settings.scheduler_poly_minutes <= 0 and settings.scheduler_books_hours <= 0:
        return None
    try:
        from app.services.scheduler import start_scheduler

        return start_scheduler(settings)
    except Exception:  # noqa: BLE001 - a broken scheduler must not take the app down
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

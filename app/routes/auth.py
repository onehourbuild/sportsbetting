"""Password gate: GET/POST /login, POST /logout.

POST /login is throttled by an in-process `LoginLimiter`: after `MAX_FAILURES_PER_IP`
consecutive wrong passwords from one client address (or `MAX_FAILURES_GLOBAL` from
anywhere) further attempts get HTTP 429 with `Retry-After` for a lockout that doubles on
every additional failure, capped at an hour.

The client address is the socket peer unless `TRUSTED_PROXY_HEADER` names a header to
trust (`fly-client-ip` on Fly.io, set in fly.toml's `[env]`). Trusting a proxy header by
default would let anyone off that proxy — `docker run`, Railway, Render — send a fresh
address with every guess and never trip the limiter; `X-Forwarded-For`'s first hop is
attacker-controlled everywhere and is never read. The value is sanitised before it reaches
a log line or the limiter's key space.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import threading
import time
from collections.abc import Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

MAX_FAILURES_PER_IP = 5
MAX_FAILURES_GLOBAL = 50
BASE_LOCKOUT_S = 30.0
MAX_LOCKOUT_S = 3600.0
GLOBAL_KEY = "*"
UNKNOWN_IP = "unknown"
MAX_IP_LENGTH = 45  # longest possible IPv6 text form
MAX_ZONE_LENGTH = 16
_NOT_ZONE_CHARS = re.compile(r"[^0-9A-Za-z_.-]")


class LoginLimiter:
    """Consecutive-failure counter with exponential lockout, per client IP and global.

    Thread-safe; state lives in the process (one machine, one owner). A successful login
    clears that IP's counter; the global counter clears only when its lockout expires
    and a login succeeds, so rotating source addresses does not reset it.
    """

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES_PER_IP,
        max_failures_global: int = MAX_FAILURES_GLOBAL,
        base_lockout_s: float = BASE_LOCKOUT_S,
        max_lockout_s: float = MAX_LOCKOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.max_failures_global = max_failures_global
        self.base_lockout_s = base_lockout_s
        self.max_lockout_s = max_lockout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, ip: str) -> float:
        """Seconds until `ip` may try again (0.0 when it may try now)."""
        now = self._clock()
        with self._lock:
            waits = [self._locked_until.get(key, 0.0) - now for key in (ip, GLOBAL_KEY)]
        return max(0.0, *waits)

    def record_failure(self, ip: str) -> float:
        """Count a wrong password; returns the lockout now in force in seconds (0 = none)."""
        now = self._clock()
        lockout = 0.0
        with self._lock:
            for key, limit in ((ip, self.max_failures), (GLOBAL_KEY, self.max_failures_global)):
                count = self._failures.get(key, 0) + 1
                self._failures[key] = count
                if count >= limit:
                    seconds = min(self.base_lockout_s * (2 ** (count - limit)), self.max_lockout_s)
                    self._locked_until[key] = now + seconds
                    lockout = max(lockout, seconds)
        return lockout

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._locked_until.pop(ip, None)
            if self._locked_until.get(GLOBAL_KEY, 0.0) <= self._clock():
                self._failures.pop(GLOBAL_KEY, None)
                self._locked_until.pop(GLOBAL_KEY, None)

    def failures(self, ip: str) -> int:
        with self._lock:
            return self._failures.get(ip, 0)


_default_limiter = LoginLimiter()


def sanitize_ip(value: str) -> str | None:
    """Normalized address text from an untrusted header, or None when it is not an IP.

    The result ends up in a WARNING log line and as a key in the limiter's dictionaries,
    so nothing that is not an address may reach either: no newlines to forge log lines
    with, no unbounded junk to grow the dictionaries with.
    """
    candidate = value.strip()[:MAX_IP_LENGTH]
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    address, _, zone = candidate.partition("%")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    zone = _NOT_ZONE_CHARS.sub("", zone)[:MAX_ZONE_LENGTH]
    return f"{parsed}%{zone}" if zone else str(parsed)


def trusted_proxy_header(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return (getattr(settings, "trusted_proxy_header", "") or "").strip().lower()


def client_ip(request: Request) -> str:
    """The client address: the socket peer, or the configured proxy header when set.

    Only the header named by `TRUSTED_PROXY_HEADER` is honoured, and only its last hop —
    the one the nearest proxy appended. Anything a client can prepend (every hop before
    that, and the whole header when no proxy header is configured) is ignored.
    """
    header = trusted_proxy_header(request)
    if header:
        raw = request.headers.get(header)
        if raw:
            cleaned = sanitize_ip(raw.split(",")[-1])
            if cleaned is not None:
                return cleaned
            # Never log the value itself: it is attacker-controlled text.
            log.warning("ignoring %s: last hop is not an IP address", header)
    return request.client.host if request.client else UNKNOWN_IP


def login_limiter(request: Request) -> LoginLimiter:
    limiter = getattr(request.app.state, "login_limiter", None)
    return limiter if isinstance(limiter, LoginLimiter) else _default_limiter


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or value == "/login":
        return "/"
    return value


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    from app.main import is_authenticated

    if is_authenticated(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)  # type: ignore[return-value]
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "next": _safe_next(next)}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
):
    from app.main import SESSION_COOKIE, SESSION_MAX_AGE_S, sign_session

    settings = request.app.state.settings
    limiter = login_limiter(request)
    ip = client_ip(request)
    wait = limiter.retry_after(ip)
    if wait > 0:
        return _too_many_attempts(request, next, wait)
    expected = settings.app_password_value
    ok = bool(expected) and hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))
    if not ok and not settings.auth_enabled:
        ok = True  # dev with no password: nothing to check
    if not ok:
        lockout = limiter.record_failure(ip)
        log.warning(
            "login failed from %s (%d consecutive%s)",
            ip,
            limiter.failures(ip),
            f"; locked out for {lockout:.0f}s" if lockout else "",
        )
        if lockout:
            return _too_many_attempts(request, next, lockout)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Wrong password.", "next": _safe_next(next)},
            status_code=401,
        )
    limiter.record_success(ip)
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(settings),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


def _too_many_attempts(request: Request, next: str, wait: float) -> HTMLResponse:
    seconds = max(1, int(wait + 0.999))
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"error": f"Too many attempts. Try again in {seconds} seconds.", "next": _safe_next(next)},
        status_code=429,
    )
    response.headers["Retry-After"] = str(seconds)
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    from app.main import SESSION_COOKIE

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response

"""Security review fixes: cookie revocation, login throttling, key redaction, no-store
headers, startup warnings, and the versioned service worker."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import APP_VERSION
from app import settings as settings_module
from app.clients.oddsapi import OddsApiClient
from app.clients.transport import FixtureTransport, HttpTransport
from app.logsetup import RedactApiKeyFilter, configure_logging, redact_api_key
from app.main import create_app, session_is_valid, sign_session
from app.models import Bet, Market
from app.routes.auth import LoginLimiter, sanitize_ip
from app.settings import Settings
from tests.conftest import FIXTURES_DIR, TEST_PASSWORD
from tests.test_routes_ui import seed_slate

STRONG_SECRET = "k" * 32


def _dev_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        app_env="dev",
        app_password="password-a",
        secret_key=STRONG_SECRET,
        demo_mode=False,
        database_url=f"sqlite:///{tmp_path / 'sec.db'}",
        fixtures_dir=FIXTURES_DIR,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- session cookie


def test_changing_the_password_or_secret_revokes_every_cookie(tmp_path: Path) -> None:
    a = _dev_settings(tmp_path)
    cookie = sign_session(a)
    assert session_is_valid(a, cookie)
    assert "ok" not in cookie.split(".")[0]  # the payload is no longer a public constant
    b = a.model_copy(update={"app_password": "password-b"})
    assert not session_is_valid(b, cookie)
    c = a.model_copy(update={"secret_key": "j" * 32})
    assert not session_is_valid(c, cookie)
    assert session_is_valid(b, sign_session(b))

    # end to end: a cookie minted under password A is rejected by an app built with B
    with TestClient(create_app(b)) as client:
        client.cookies.set("session", cookie)
        assert client.get("/bets", follow_redirects=False).status_code == 303
        client.cookies.set("session", sign_session(b))
        assert client.get("/bets", follow_redirects=False).status_code == 200


# --------------------------------------------------------------------------- login throttling


def test_login_locks_out_after_repeated_failures(
    app: FastAPI, anon_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [1000.0]
    limiter = LoginLimiter(clock=lambda: clock[0])
    app.state.login_limiter = limiter
    with caplog.at_level(logging.WARNING, logger="app.routes.auth"):
        codes = [
            anon_client.post(
                "/login", data={"password": "nope"}, follow_redirects=False
            ).status_code
            for _ in range(5)
        ]
    assert codes == [401, 401, 401, 401, 429]
    assert any("login failed from" in r.getMessage() for r in caplog.records)

    # even the right password is refused while the lockout lasts
    blocked = anon_client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "30"
    assert "Too many attempts" in blocked.text
    assert "session" not in anon_client.cookies

    # a spoofed proxy header buys nothing while TRUSTED_PROXY_HEADER is empty: the lockout
    # follows the socket peer, so this is still 429 (it used to be a free 401)
    spoofed = anon_client.post(
        "/login",
        data={"password": "nope"},
        headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "Fly-Client-IP": "203.0.113.9"},
        follow_redirects=False,
    )
    assert spoofed.status_code == 429

    # after the window the right password works and the counter resets
    clock[0] += 31
    ok = anon_client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert ok.status_code == 303 and "session" in anon_client.cookies
    assert limiter.failures("testclient") == 0


def test_login_limiter_backs_off_exponentially_and_caps() -> None:
    clock = [0.0]
    limiter = LoginLimiter(
        max_failures=2, base_lockout_s=10.0, max_lockout_s=25.0, clock=lambda: clock[0]
    )
    assert limiter.record_failure("ip") == 0.0
    assert limiter.record_failure("ip") == 10.0  # threshold reached
    assert limiter.retry_after("ip") == pytest.approx(10.0)
    clock[0] += 10.0
    assert limiter.retry_after("ip") == 0.0
    assert limiter.record_failure("ip") == 20.0  # doubles
    clock[0] += 20.0
    assert limiter.record_failure("ip") == 25.0  # capped
    limiter.record_success("ip")
    assert limiter.retry_after("ip") == 0.0 and limiter.failures("ip") == 0


def test_login_limiter_global_counter_stops_address_rotation() -> None:
    limiter = LoginLimiter(max_failures=100, max_failures_global=3, base_lockout_s=5.0)
    for ip in ("1.1.1.1", "2.2.2.2"):
        assert limiter.record_failure(ip) == 0.0
    assert limiter.record_failure("3.3.3.3") == 5.0
    assert limiter.retry_after("4.4.4.4") > 0  # a fresh address is locked out too


def test_proxy_headers_are_ignored_unless_one_is_configured(tmp_path: Path) -> None:
    """Off Fly (docker run, Railway, Render) anyone can set Fly-Client-IP. Trusting it by
    default gave an attacker a fresh identity per guess and never tripped the limiter."""
    settings = _dev_settings(tmp_path, app_password=TEST_PASSWORD)
    assert settings.trusted_proxy_header == ""
    with TestClient(create_app(settings)) as client:
        codes = [
            client.post(
                "/login",
                data={"password": "nope"},
                headers={"Fly-Client-IP": f"203.0.113.{i}", "X-Forwarded-For": f"198.51.100.{i}"},
                follow_redirects=False,
            ).status_code
            for i in range(1, 8)
        ]
    assert codes[:4] == [401] * 4
    assert codes[4:] == [429] * 3  # the rotation buys nothing: it is all one socket peer


def test_a_configured_proxy_header_is_honoured_and_sanitised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _dev_settings(
        tmp_path, app_password=TEST_PASSWORD, trusted_proxy_header="Fly-Client-IP"
    )
    assert settings.trusted_proxy_header == "fly-client-ip"  # normalised
    with TestClient(create_app(settings)) as client:

        def attempt(ip: str) -> int:
            return client.post(
                "/login",
                data={"password": "nope"},
                headers={"Fly-Client-IP": ip},
                follow_redirects=False,
            ).status_code

        assert [attempt("203.0.113.5") for _ in range(5)] == [401, 401, 401, 401, 429]
        assert attempt("203.0.113.6") == 401  # a different real address is its own counter

        # the header is attacker-supplied: a value that is not an address is dropped, so
        # nothing it carries can forge a log line or grow the limiter's key space
        with caplog.at_level(logging.WARNING, logger="app.routes.auth"):
            assert attempt("10.0.0.9\nWARNING:root:spoofed log line" + "9" * 200) == 401
        messages = [r.getMessage() for r in caplog.records]
        assert all("\n" not in line for line in messages)
        assert "spoofed log line" not in "".join(messages)
        assert any("ignoring fly-client-ip" in line for line in messages)
        assert any("login failed from testclient" in line for line in messages)  # socket peer

    assert sanitize_ip("  203.0.113.9 ") == "203.0.113.9"
    assert sanitize_ip("2001:db8::1%eth0") == "2001:db8::1%eth0"
    assert sanitize_ip("[2001:db8::1]") == "2001:db8::1"
    assert sanitize_ip("../../etc/passwd") is None
    assert sanitize_ip("") is None and sanitize_ip("<script>alert(1)</script>") is None
    assert sanitize_ip("203.0.113.9:4321") is None  # not an address either
    assert sanitize_ip("1" * 200) is None


def test_only_the_last_proxy_hop_is_read(tmp_path: Path) -> None:
    """A client can prepend anything to X-Forwarded-For; only the hop the nearest proxy
    appended means anything, so the first hop is never used."""
    settings = _dev_settings(
        tmp_path, app_password=TEST_PASSWORD, trusted_proxy_header="x-forwarded-for"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        for _ in range(5):
            client.post(
                "/login",
                data={"password": "nope"},
                headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"},
                follow_redirects=False,
            )
        limiter = app.state.login_limiter
        assert limiter.failures("203.0.113.7") == 5
        assert limiter.failures("1.2.3.4") == 0
        # a fresh first hop with the same real address stays locked out
        spoofed = client.post(
            "/login",
            data={"password": "nope"},
            headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.7"},
            follow_redirects=False,
        )
        assert spoofed.status_code == 429


# --------------------------------------------------------------------------- config errors


def test_a_bad_production_config_never_prints_the_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_require_password_outside_dev` raises inside a pydantic validator, and
    `str(ValidationError)` renders `input_value=<the whole env dict>`. Settings is built at
    import time in app/main.py, so an uncaught one prints every secret to `fly logs`."""
    key = "ODDSKEY-0123456789abcdef0123456789"
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_PASSWORD", "too-short")
    monkeypatch.setenv("SECRET_KEY", "s3cr3t-key-that-is-also-a-secret")
    monkeypatch.setenv("ODDS_API_KEY", key)
    monkeypatch.setitem(settings_module.Settings.model_config, "env_file", None)
    previous = settings_module._active
    settings_module.set_settings(None)  # read the environment, not an app's Settings
    settings_module._settings_from_env.cache_clear()
    try:
        with pytest.raises(SystemExit) as excinfo:
            settings_module.get_settings()
    finally:
        settings_module._settings_from_env.cache_clear()
        settings_module.set_settings(previous)

    message = str(excinfo.value)
    assert "APP_PASSWORD must be at least 12 characters" in message
    assert "Invalid configuration" in message
    for secret in (key, "too-short", "s3cr3t-key-that-is-also-a-secret"):
        assert secret not in message
    assert "input_value" not in message and "ValidationError" not in message


def test_a_startup_config_error_reaches_stderr_without_the_key() -> None:
    """End to end: `python -c "import app.main"` with a broken prod config."""
    import os
    import subprocess
    import sys

    key = "SUPERSECRETKEY-0123456789abcdef"
    env = {
        **os.environ,
        "APP_ENV": "prod",
        "APP_PASSWORD": "short",
        "ODDS_API_KEY": key,
        "SECRET_KEY": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Invalid configuration" in output and "APP_PASSWORD" in output
    assert key not in output and "input_value" not in output
    assert "Traceback" not in output


def test_secrets_never_render_in_a_settings_repr(tmp_path: Path) -> None:
    settings = _dev_settings(
        tmp_path, app_password="hunter2-hunter2", odds_api_key="ODDSKEY-abcdef"
    )
    for text in (repr(settings), str(settings), repr(settings.app_password)):
        assert "hunter2" not in text and "ODDSKEY" not in text
    assert settings.app_password_value == "hunter2-hunter2"
    assert settings.odds_api_key_value == "ODDSKEY-abcdef"
    assert settings.secret_key_value == STRONG_SECRET
    # the Odds API client unwraps it: a SecretStr must never reach the query string
    transport = FixtureTransport(FIXTURES_DIR, [])
    assert OddsApiClient(transport, settings.odds_api_key).api_key == "ODDSKEY-abcdef"
    assert "ODDSKEY" not in repr(OddsApiClient(transport, settings.odds_api_key))


# --------------------------------------------------------------------------- CSRF


def test_cross_site_state_changing_requests_are_rejected(client: TestClient) -> None:
    """SameSite=Lax is the only other protection, and /scan?kind=books spends credits."""
    cross_site = client.post("/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_site.status_code == 403
    assert "session" in client.cookies  # nothing happened

    other_origin = client.post("/logout", headers={"Origin": "https://evil.example"})
    assert other_origin.status_code == 403
    by_referer = client.post("/logout", headers={"Referer": "https://evil.example/page"})
    assert by_referer.status_code == 403
    opaque = client.post("/logout", headers={"Origin": "null"})
    assert opaque.status_code == 403

    # reads are never blocked, whoever links to them
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    # ...and neither is the login form itself
    assert client.get("/login", headers={"Origin": "https://evil.example"}).status_code in (
        200,
        303,
    )


def test_same_origin_and_header_less_posts_still_work(client: TestClient) -> None:
    host = client.base_url.host
    same_origin = client.post(
        "/logout",
        headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert same_origin.status_code == 303  # htmx sends Origin on POST; this is the real path
    assert "session" not in client.cookies

    client.post("/login", data={"password": TEST_PASSWORD})
    assert client.post("/logout", follow_redirects=False).status_code == 303  # no headers at all


def test_security_headers_on_every_response(
    anon_client: TestClient, client: TestClient, tmp_path: Path
) -> None:
    for response in (anon_client.get("/login"), anon_client.get("/healthz"), client.get("/")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        # dev is http://localhost: HSTS there would pin the whole host to HTTPS
        assert "strict-transport-security" not in response.headers

    prod = _dev_settings(
        tmp_path, app_env="prod", app_password="correct-horse-battery", secret_key=STRONG_SECRET
    )
    assert prod.cookie_secure is True
    with TestClient(create_app(prod)) as prod_client:
        for path in ("/login", "/healthz", "/static/app.css"):
            response = prod_client.get(path)
            assert response.headers["strict-transport-security"] == (
                "max-age=31536000; includeSubDomains"
            ), path
            assert response.headers["x-content-type-options"] == "nosniff", path


# --------------------------------------------------------------------------- key redaction


def test_odds_api_key_is_redacted_from_http_client_logs(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """httpx logs every request URL (with the query string) at INFO; the key must never
    reach the log even when that logger is turned all the way up."""
    key = "SUPERSECRETKEY123"

    def handler(request: httpx.Request) -> httpx.Response:
        assert key in str(request.url)  # it is really sent
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "10"})

    transport = HttpTransport(httpx.Client(transport=httpx.MockTransport(handler)))
    client = OddsApiClient(transport, key)
    with caplog.at_level(logging.DEBUG, logger="httpx"):
        client.odds("nfl", ["pinnacle"])
    assert key not in caplog.text
    request_lines = [r.getMessage() for r in caplog.records if "HTTP Request" in r.getMessage()]
    assert request_lines and all("apiKey=***" in line for line in request_lines)


def test_redaction_filter_and_quiet_loggers() -> None:
    configure_logging("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert any(isinstance(f, RedactApiKeyFilter) for f in logging.getLogger("httpx").filters)
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "GET %s", ("https://h/o?apiKey=abc123&markets=h2h",), None
    )
    assert RedactApiKeyFilter().filter(record) is True
    assert record.getMessage() == "GET https://h/o?apiKey=***&markets=h2h"
    assert redact_api_key('url="https://h/o?apikey=Zz9"') == 'url="https://h/o?apikey=***"'


# --------------------------------------------------------------------------- no-store


def test_authenticated_responses_are_never_cacheable(
    client: TestClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.types import ScanResult
    from app.services import scan as scan_service

    slate = seed_slate(db_session)
    paths = (
        "/",
        "/bets",
        "/settings",
        "/diagnostics",
        "/api/opportunities",
        f"/bets/new?opportunity_id={slate['o_kc'].id}",
        f"/games/{slate['g_nfl'].id}",
    )
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store", path
        assert response.headers["pragma"] == "no-cache", path

    result = ScanResult(
        scan_id=slate["scan"].id,
        kind="poly",
        leagues=["nfl"],
        n_markets=1,
        n_matched=1,
        n_opps=1,
        credits_used=None,
        credits_remaining=None,
    )
    monkeypatch.setattr(scan_service, "run_scan_default", lambda *a, **k: result)
    partial = client.post("/scan?kind=poly", headers={"HX-Request": "true"})
    assert partial.status_code == 200 and partial.headers["cache-control"] == "no-store"

    # public assets keep their own caching rules
    assert "no-store" not in client.get("/static/app.css").headers.get("cache-control", "")
    assert client.get("/sw.js").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/healthz").headers


# --------------------------------------------------------------------------- startup warnings


def test_startup_warns_when_auth_is_disabled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _dev_settings(tmp_path, app_password="", secret_key="dev-secret-change-me")
    assert settings.auth_enabled is False
    with caplog.at_level(logging.WARNING, logger="app.main"):
        create_app(settings)
    assert any("AUTH DISABLED" in r.getMessage() for r in caplog.records)


def test_startup_warns_when_demo_bets_linger_with_demo_mode_off(
    settings: Settings, engine, db_session, caplog: pytest.LogCaptureFixture
) -> None:
    db_session.add(Market(id="500701", market_type="moneyline"))
    db_session.add(
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
            notes="demo",
        )
    )
    db_session.commit()
    with caplog.at_level(logging.WARNING, logger="app.main"), TestClient(create_app(settings)):
        pass
    assert any("demo-clear" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- static versioning


def test_static_assets_and_service_worker_cache_are_versioned(
    client: TestClient, anon_client: TestClient
) -> None:
    home = client.get("/").text
    assert f'href="/static/app.css?v={APP_VERSION}"' in home
    assert f'src="/static/app.js?v={APP_VERSION}"' in home
    assert f'src="/static/htmx.min.js?v={APP_VERSION}"' in home
    login = anon_client.get("/login").text
    assert f'href="/static/app.css?v={APP_VERSION}"' in login

    sw = anon_client.get("/sw.js").text
    assert "__APP_VERSION__" not in sw
    assert f'const VERSION = "{APP_VERSION}"' in sw
    assert 'const CACHE = "edges-v2-" + VERSION' in sw
    assert "Stale-while-revalidate" in sw and "event.waitUntil(refresh" in sw


def test_theme_color_follows_the_color_scheme(client: TestClient, anon_client: TestClient) -> None:
    light = '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f5f7fa">'
    dark = '<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0b0f14">'
    for html in (client.get("/").text, anon_client.get("/login").text):
        assert light in html and dark in html
        assert '<meta name="theme-color" content=' not in html

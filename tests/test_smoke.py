"""App boots, auth gate works, PWA and static assets are public."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.settings import Settings


def test_healthz_is_public(anon_client: TestClient) -> None:
    response = anon_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["demo"] is False


def test_unauthenticated_home_redirects_to_login(anon_client: TestClient) -> None:
    response = anon_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_unauthenticated_htmx_request_gets_hx_redirect(anon_client: TestClient) -> None:
    response = anon_client.get("/bets", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_login_page_renders(anon_client: TestClient) -> None:
    response = anon_client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text


def test_login_wrong_password_stays_on_login(anon_client: TestClient) -> None:
    response = anon_client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert response.status_code == 401
    assert "Wrong password" in response.text
    assert "session" not in anon_client.cookies
    still_blocked = anon_client.get("/", follow_redirects=False)
    assert still_blocked.status_code == 303


def test_login_ok_then_home_is_200(anon_client: TestClient) -> None:
    response = anon_client.post("/login", data={"password": "test"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers.get("set-cookie", "")
    assert "session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie  # dev
    home = anon_client.get("/")
    assert home.status_code == 200
    assert "Edges" in home.text


def test_every_screen_renders_when_logged_in(client: TestClient) -> None:
    for path in ("/", "/bets", "/settings", "/diagnostics"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "bottom-nav" in response.text, path
        assert 'href="/manifest.webmanifest"' in response.text, path


def test_missing_game_is_404_page(client: TestClient) -> None:
    response = client.get("/games/999999")
    assert response.status_code == 404
    assert "text/html" in response.headers.get("content-type", "")


def test_logout_clears_session(client: TestClient) -> None:
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert "session" not in client.cookies
    assert client.get("/", follow_redirects=False).status_code == 303


def test_manifest_and_sw_served_without_auth(anon_client: TestClient) -> None:
    manifest = anon_client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    body = manifest.json()
    assert body["name"] == "Edge Finder"
    assert body["short_name"] == "Edges"
    assert body["display"] == "standalone"
    assert {icon["sizes"] for icon in body["icons"]} == {"192x192", "512x512"}

    sw = anon_client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["content-type"].startswith("application/javascript")
    assert sw.headers["service-worker-allowed"] == "/"
    assert "edges-v2" in sw.text
    assert "OFFLINE_HTML" in sw.text and "Network-only for pages" in sw.text


def test_static_assets_served_without_auth(anon_client: TestClient) -> None:
    css = anon_client.get("/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "--bg:" in css.text
    for path in (
        "/static/htmx.min.js",
        "/static/app.js",
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/apple-touch-icon-180.png",
    ):
        assert anon_client.get(path).status_code == 200, path


STRONG_PASSWORD = "correct-horse-battery-staple"
STRONG_SECRET = "f" * 64


def test_settings_requires_password_outside_dev() -> None:
    import pytest

    with pytest.raises(ValueError, match="APP_PASSWORD"):
        Settings(_env_file=None, app_env="prod", app_password="", secret_key=STRONG_SECRET)  # type: ignore[call-arg]
    ok = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="prod",
        app_password=STRONG_PASSWORD,
        secret_key=STRONG_SECRET,
    )
    assert ok.auth_enabled and ok.cookie_secure


def test_settings_requires_a_long_password_outside_dev() -> None:
    import pytest

    with pytest.raises(ValueError, match="at least 12 characters"):
        Settings(_env_file=None, app_env="prod", app_password="x", secret_key=STRONG_SECRET)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="at least 12 characters"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, app_env="prod", app_password="short-pw-11", secret_key=STRONG_SECRET
        )
    # dev keeps accepting anything (local only)
    dev = Settings(_env_file=None, app_env="dev", app_password="x")  # type: ignore[call-arg]
    assert dev.auth_enabled


def test_settings_rejects_the_default_or_short_secret_key_outside_dev() -> None:
    """The default SECRET_KEY is public: with it anyone can mint a valid session cookie."""
    import pytest

    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(_env_file=None, app_env="prod", app_password=STRONG_PASSWORD)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="prod",
            app_password=STRONG_PASSWORD,
            secret_key="dev-secret-change-me",
        )
    with pytest.raises(ValueError, match="at least 32 characters"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="prod",
            app_password=STRONG_PASSWORD,
            secret_key="only-thirty-one-characters-xxxx",
        )
    ok = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="prod",
        app_password=STRONG_PASSWORD,
        secret_key="a" * 32,
    )
    # SecretStr: the value is only readable through the accessor, never from repr()
    assert ok.secret_key_value == "a" * 32
    assert "a" * 32 not in repr(ok) and "a" * 32 not in str(ok.secret_key)
    # dev still boots on the default key
    dev = Settings(_env_file=None, app_env="dev")  # type: ignore[call-arg]
    assert dev.secret_key_value == "dev-secret-change-me"


def test_auth_disabled_in_dev_without_password(tmp_path) -> None:
    from app.main import create_app

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="dev",
        app_password="",
        database_url=f"sqlite:///{tmp_path / 'open.db'}",
    )
    assert settings.auth_enabled is False
    with TestClient(create_app(settings)) as client:
        assert client.get("/", follow_redirects=False).status_code == 200

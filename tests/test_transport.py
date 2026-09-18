"""FixtureTransport routing/recording and HttpTransport retry behaviour (httpx.MockTransport)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.clients.transport import (
    USER_AGENT,
    FixtureTransport,
    HttpTransport,
    Transport,
    TransportError,
    load_fixture,
)

# --------------------------------------------------------------------------- fixtures


def test_fixture_transport_routes_files_and_records_calls(tmp_path: Path) -> None:
    (tmp_path / "events.json").write_text(json.dumps({"events": [1, 2]}), encoding="utf-8")
    transport = FixtureTransport(
        tmp_path, [("GET", "https://gamma-api.polymarket.com/events", "events.json")]
    )
    assert isinstance(transport, Transport)
    payload, headers = transport.get_json(
        "https://gamma-api.polymarket.com/events", params={"tag_slug": "nfl"}
    )
    assert payload == {"events": [1, 2]}
    assert dict(headers) == {}
    assert transport.calls == [
        {
            "method": "GET",
            "url": "https://gamma-api.polymarket.com/events",
            "params": {"tag_slug": "nfl"},
            "body": None,
        }
    ]


def test_fixture_transport_callable_route_receives_url_params_body(tmp_path: Path) -> None:
    seen: list[tuple] = []

    def handler(url, params, body):
        seen.append((url, params, body))
        return [{"asset_id": t["token_id"]} for t in body], {"x-test": "1"}

    transport = FixtureTransport(
        tmp_path,
        [("POST", "https://clob.polymarket.com/books", handler)],
        default_headers={"x-default": "d"},
    )
    body = [{"token_id": "1"}, {"token_id": "2"}]
    payload, headers = transport.post_json("https://clob.polymarket.com/books", body)
    assert payload == [{"asset_id": "1"}, {"asset_id": "2"}]
    assert headers == {"x-default": "d", "x-test": "1"}
    assert seen == [("https://clob.polymarket.com/books", None, body)]
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["body"] == body


def test_fixture_transport_matches_routes_in_order(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('"a"', encoding="utf-8")
    (tmp_path / "b.json").write_text('"b"', encoding="utf-8")
    transport = FixtureTransport(
        tmp_path,
        [
            ("GET", "https://x/markets/5", "a.json"),
            ("GET", "https://x/markets", "b.json"),
        ],
    )
    assert transport.get_json("https://x/markets/55")[0] == "a"
    assert transport.get_json("https://x/markets?slug=q")[0] == "b"
    # method must match too
    with pytest.raises(TransportError):
        transport.post_json("https://x/markets", {})


def test_fixture_transport_unknown_route_raises_404(tmp_path: Path) -> None:
    transport = FixtureTransport(tmp_path, [])
    with pytest.raises(TransportError) as excinfo:
        transport.get_json("https://nowhere.example/x")
    assert excinfo.value.status == 404
    assert excinfo.value.url == "https://nowhere.example/x"
    assert "status=404" in str(excinfo.value)
    assert len(transport.calls) == 1  # unknown calls are still recorded


def test_load_fixture_reads_json(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.json").write_text('{"k": 1}', encoding="utf-8")
    assert load_fixture("sub/x.json", tmp_path) == {"k": 1}


# --------------------------------------------------------------------------- http


def _http(handler, retries: int = 2) -> tuple[HttpTransport, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpTransport(client, retries=retries, sleep=sleeps.append), sleeps


def test_http_transport_success_returns_json_and_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == USER_AGENT
        assert request.url.params["apiKey"] == "k"
        return httpx.Response(200, json={"ok": 1}, headers={"x-requests-remaining": "499"})

    transport, sleeps = _http(handler)
    payload, headers = transport.get_json("https://api.example/v4/sports", params={"apiKey": "k"})
    assert payload == {"ok": 1}
    assert headers["x-requests-remaining"] == "499"
    assert sleeps == []


def test_http_transport_retries_on_5xx_then_succeeds() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="try later")
        return httpx.Response(200, json=[1])

    transport, sleeps = _http(handler)
    payload, _ = transport.get_json("https://api.example/x")
    assert payload == [1]
    assert len(attempts) == 3
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)


def test_http_transport_gives_up_after_retries_on_5xx() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, text="boom")

    transport, _ = _http(handler)
    with pytest.raises(TransportError) as excinfo:
        transport.get_json("https://api.example/x")
    assert excinfo.value.status == 500
    assert len(attempts) == 3  # 1 + 2 retries


def test_http_transport_retries_timeouts_and_connection_errors() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if len(attempts) == 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"after": "retries"})

    transport, sleeps = _http(handler)
    payload, _ = transport.post_json("https://clob.example/books", [{"token_id": "1"}])
    assert payload == {"after": "retries"}
    assert len(attempts) == 3
    assert len(sleeps) == 2


def test_http_transport_timeout_exhausted_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("nope", request=request)

    transport, _ = _http(handler)
    with pytest.raises(TransportError) as excinfo:
        transport.get_json("https://api.example/x")
    assert excinfo.value.status is None
    assert "ConnectTimeout" in str(excinfo.value)


def test_http_transport_does_not_retry_4xx() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"message": "bad key"})

    transport, sleeps = _http(handler)
    with pytest.raises(TransportError) as excinfo:
        transport.get_json("https://api.example/x")
    assert excinfo.value.status == 401
    assert "bad key" in str(excinfo.value)
    assert len(attempts) == 1
    assert sleeps == []


def test_http_transport_post_sends_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content) == [{"token_id": "abc"}]
        return httpx.Response(200, json=[{"asset_id": "abc"}])

    transport, _ = _http(handler)
    payload, _ = transport.post_json("https://clob.example/books", [{"token_id": "abc"}])
    assert payload == [{"asset_id": "abc"}]


def test_http_transport_non_json_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    transport, _ = _http(handler)
    with pytest.raises(TransportError, match="Non-JSON"):
        transport.get_json("https://api.example/x")

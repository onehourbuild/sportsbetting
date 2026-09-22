"""Transport layer: a Protocol, a real httpx implementation, and a fixture-backed fake.

Every client takes a `Transport` so tests and demo mode never touch the network.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

log = logging.getLogger(__name__)

# ESPN's edge (site.api.espn.com) allowlists User-Agents that name a recognized HTTP
# client and 403s anything else: "polymarket-edge-finder/0.1" was refused on every
# request while "curl/8.4.0", "python-requests/..." and "python-httpx/..." were served.
# Leading with the real client token gets us served without pretending to be a browser,
# and the app still identifies itself. Verified live 2026-09-18; docs/RESEARCH.md item 20.
USER_AGENT = f"python-httpx/{httpx.__version__} polymarket-edge-finder/0.1 (+personal tool)"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_RETRIES = 2
RETRY_BACKOFF_S = (0.25, 0.75)

JsonHeaders = tuple[Any, Mapping[str, str]]
RouteHandler = Callable[[str, Mapping[str, Any] | None, Any], JsonHeaders]
Route = tuple[str, str, "str | RouteHandler"]


class TransportError(Exception):
    """Raised for non-2xx responses, exhausted retries, and unknown fixture routes."""

    def __init__(self, message: str, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.url = url

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.url:
            parts.append(f"url={self.url}")
        return " ".join(parts)


@runtime_checkable
class Transport(Protocol):
    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]: ...

    def post_json(
        self,
        url: str,
        body: Any,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]: ...


# --------------------------------------------------------------------------- HTTP


class HttpTransport:
    """httpx-backed transport.

    15 s timeout; up to 2 retries with a small backoff on 5xx, timeouts and connection errors.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        backoff: Sequence[float] = RETRY_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self._owns_client = client is None
        self.retries = max(0, int(retries))
        self._backoff = tuple(backoff)
        self._sleep = sleep

    # -- Transport protocol --------------------------------------------------

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        return self._request("GET", url, params=params, headers=headers)

    def post_json(
        self,
        url: str,
        body: Any,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        return self._request("POST", url, json_body=body, headers=headers)

    # -- internals -----------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        merged_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        attempts = self.retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=json_body if method == "POST" else None,
                    headers=merged_headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning(
                    "%s %s failed (%s), attempt %d/%d", method, url, exc, attempt + 1, attempts
                )
                if attempt < attempts - 1:
                    self._pause(attempt)
                    continue
                raise TransportError(f"{type(exc).__name__}: {exc}", status=None, url=url) from exc

            if response.status_code >= 500 and attempt < attempts - 1:
                log.warning(
                    "%s %s -> %d, retrying (%d/%d)",
                    method,
                    url,
                    response.status_code,
                    attempt + 1,
                    attempts,
                )
                self._pause(attempt)
                continue

            if response.status_code >= 400:
                snippet = response.text[:200]
                raise TransportError(
                    f"HTTP {response.status_code} for {method} {url}: {snippet}",
                    status=response.status_code,
                    url=url,
                )

            try:
                payload = response.json() if response.content else None
            except ValueError as exc:
                raise TransportError(
                    f"Non-JSON response for {method} {url}", status=response.status_code, url=url
                ) from exc
            return payload, dict(response.headers)

        # unreachable in practice; keeps type checkers honest
        raise TransportError(f"request failed: {last_error}", status=None, url=url)

    def _pause(self, attempt: int) -> None:
        if not self._backoff:
            return
        delay = self._backoff[min(attempt, len(self._backoff) - 1)]
        if delay > 0:
            self._sleep(delay)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- fixtures


def load_fixture(name: str, fixtures_dir: Path | str | None = None) -> Any:
    """Load `<fixtures_dir>/<name>` (JSON). `name` may include subdirectories."""
    if fixtures_dir is None:
        from app.settings import get_settings

        fixtures_dir = get_settings().fixtures_dir
    path = Path(fixtures_dir) / name
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class FixtureTransport:
    """Serves JSON files (or callables) from `fixtures_dir` by (method, url_prefix) routes.

    `routes` is a list of `(method, url_prefix, filename_or_callable)`, matched in order.
    A callable receives `(url, params, body)` and returns `(json, headers)`. Unknown routes
    raise `TransportError(status=404)`. Every call is recorded in `.calls` as
    `{"method", "url", "params", "body"}`.
    """

    def __init__(
        self,
        fixtures_dir: Path | str,
        routes: Sequence[Route] | None = None,
        *,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.fixtures_dir = Path(fixtures_dir)
        self.routes: list[Route] = list(routes or [])
        self.calls: list[dict[str, Any]] = []
        self.default_headers: dict[str, str] = dict(default_headers or {})

    def add_route(self, method: str, url_prefix: str, target: str | RouteHandler) -> None:
        self.routes.append((method, url_prefix, target))

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        return self._dispatch("GET", url, params, None)

    def post_json(
        self,
        url: str,
        body: Any,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        return self._dispatch("POST", url, None, body)

    def _dispatch(
        self, method: str, url: str, params: Mapping[str, Any] | None, body: Any
    ) -> tuple[Any, Mapping[str, str]]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params) if params else None,
                "body": body,
            }
        )
        for route_method, prefix, target in self.routes:
            if route_method.upper() != method or not url.startswith(prefix):
                continue
            if callable(target):
                payload, headers = target(url, params, body)
                merged = dict(self.default_headers)
                merged.update(headers or {})
                return payload, merged
            return load_fixture(target, self.fixtures_dir), dict(self.default_headers)
        raise TransportError(f"no fixture route for {method} {url}", status=404, url=url)


__all__ = [
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT_S",
    "USER_AGENT",
    "FixtureTransport",
    "HttpTransport",
    "Route",
    "RouteHandler",
    "Transport",
    "TransportError",
    "load_fixture",
]

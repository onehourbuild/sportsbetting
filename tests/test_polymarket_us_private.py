"""Signing and reading the owner's own Polymarket US fills.

This module holds a private key, so the tests care about two things beyond "does it
parse": that a signature actually verifies against the matching public key, and that no
failure path echoes the credential. This project has already leaked one API key into a log
through a traceback (docs/DECISIONS.md), and a signing secret is worse.

The one thing no test here can settle is whether the live API signs the path with or
without its query string. That is recorded in the module docstring; a wrong answer fails
authentication loudly, which is the right way for an unverified assumption to fail.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.clients.polymarket_us_private import (
    ACTIVITIES_PATH,
    PolymarketUsAuthError,
    PolymarketUsPrivateClient,
    auth_headers,
    load_signing_key,
    signature_for,
    signing_payload,
)

SECRET_SEED = bytes(range(32))
SECRET_B64 = base64.b64encode(SECRET_SEED).decode()
KEY_ID = "5b1f9c6e-0000-4a00-9000-abcdefabcdef"


class RecordingTransport:
    """Captures headers, which FixtureTransport deliberately discards."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        page = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]
        return page, {}

    def post_json(self, *args, **kwargs):  # pragma: no cover - never used
        raise AssertionError("the private client must never POST: it reads, it does not trade")


# ------------------------------------------------------------------------- key loading


@pytest.mark.parametrize(
    "secret",
    [
        SECRET_B64,
        base64.urlsafe_b64encode(SECRET_SEED).decode(),
        SECRET_SEED.hex(),
        base64.b64encode(SECRET_SEED + bytes(32)).decode(),  # seed + public half
    ],
    ids=["base64", "base64url", "hex", "expanded"],
)
def test_loads_the_shapes_an_ed25519_secret_is_handed_out_in(secret: str) -> None:
    key = load_signing_key(secret)
    assert isinstance(key, Ed25519PrivateKey)
    assert key.private_bytes_raw() == SECRET_SEED


def test_loads_a_pem_secret() -> None:
    pem = (
        Ed25519PrivateKey.from_private_bytes(SECRET_SEED)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    assert load_signing_key(pem).private_bytes_raw() == SECRET_SEED


def test_an_empty_secret_says_so_plainly() -> None:
    with pytest.raises(PolymarketUsAuthError, match="no Polymarket US API secret"):
        load_signing_key("")


def test_a_wrong_length_key_names_the_length_not_the_value() -> None:
    """The value is the credential, so it must never appear in the message."""
    secret = base64.b64encode(b"far too short").decode()
    with pytest.raises(PolymarketUsAuthError) as caught:
        load_signing_key(secret)

    message = str(caught.value)
    assert "13 bytes" in message
    assert secret not in message and "far too short" not in message


def test_unrecognisable_secrets_are_rejected_without_echoing_them() -> None:
    with pytest.raises(PolymarketUsAuthError) as caught:
        load_signing_key("not-a-key-at-all!!")
    assert "neither PEM, base64 nor hex" in str(caught.value)
    assert "not-a-key-at-all" not in str(caught.value)


# ---------------------------------------------------------------------------- signing


def test_the_signature_verifies_against_the_matching_public_key() -> None:
    """The only test that proves the signing is real rather than merely deterministic."""
    key = load_signing_key(SECRET_B64)
    stamp = 1_758_000_000_000

    signature = signature_for(key, stamp, "GET", ACTIVITIES_PATH)

    key.public_key().verify(
        base64.b64decode(signature), signing_payload(stamp, "GET", ACTIVITIES_PATH)
    )


def test_the_payload_is_timestamp_then_method_then_path() -> None:
    assert signing_payload(1700, "get", "/v1/x") == b"1700GET/v1/x"


def test_a_different_timestamp_or_path_changes_the_signature() -> None:
    key = load_signing_key(SECRET_B64)
    base = signature_for(key, 1000, "GET", ACTIVITIES_PATH)

    assert signature_for(key, 1001, "GET", ACTIVITIES_PATH) != base
    assert signature_for(key, 1000, "GET", "/v1/other") != base
    assert signature_for(key, 1000, "POST", ACTIVITIES_PATH) != base


def test_auth_headers_carry_the_key_id_and_a_millisecond_timestamp() -> None:
    key = load_signing_key(SECRET_B64)

    headers = auth_headers(KEY_ID, key, "GET", ACTIVITIES_PATH)

    assert headers["X-PM-Access-Key"] == KEY_ID
    stamp = int(headers["X-PM-Timestamp"])
    now_ms = datetime.now(UTC).timestamp() * 1000
    # Milliseconds, and close enough to now to survive the API's 30s window.
    assert abs(stamp - now_ms) < 5_000
    key.public_key().verify(
        base64.b64decode(headers["X-PM-Signature"]),
        signing_payload(stamp, "GET", ACTIVITIES_PATH),
    )


def test_the_timestamp_is_generated_per_request_not_reused() -> None:
    """The stamp is bound into the signature, so a cached pair would go stale together."""
    key = load_signing_key(SECRET_B64)
    first = auth_headers(KEY_ID, key, "GET", ACTIVITIES_PATH, timestamp_ms=1)
    second = auth_headers(KEY_ID, key, "GET", ACTIVITIES_PATH, timestamp_ms=2)

    assert first["X-PM-Signature"] != second["X-PM-Signature"]


# ----------------------------------------------------------------------------- reading


def _client(pages: list[dict]) -> tuple[PolymarketUsPrivateClient, RecordingTransport]:
    transport = RecordingTransport(pages)
    return PolymarketUsPrivateClient(transport, KEY_ID, SECRET_B64), transport


def test_requires_both_halves_of_the_credential() -> None:
    with pytest.raises(PolymarketUsAuthError, match="no Polymarket US API key id"):
        PolymarketUsPrivateClient(RecordingTransport([]), "", SECRET_B64)
    with pytest.raises(PolymarketUsAuthError, match="no Polymarket US API secret"):
        PolymarketUsPrivateClient(RecordingTransport([]), KEY_ID, "")


def test_reads_trades_and_asks_only_for_trades() -> None:
    client, transport = _client([{"activities": [{"id": "a1"}, {"id": "a2"}], "eof": True}])

    trades = client.trades()

    assert [t["id"] for t in trades] == ["a1", "a2"]
    assert transport.calls[0]["params"]["types"] == "ACTIVITY_TYPE_TRADE"
    assert transport.calls[0]["url"].endswith(ACTIVITIES_PATH)


def test_every_request_is_signed() -> None:
    client, transport = _client([{"activities": [], "eof": True}])

    client.trades()

    headers = transport.calls[0]["headers"]
    assert set(headers) == {"X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature"}


def test_follows_the_cursor_until_eof() -> None:
    client, transport = _client(
        [
            {"activities": [{"id": "a1"}], "nextCursor": "c2", "eof": False},
            {"activities": [{"id": "a2"}], "eof": True},
        ]
    )

    trades = client.trades()

    assert [t["id"] for t in trades] == ["a1", "a2"]
    assert transport.calls[1]["params"]["cursor"] == "c2"


def test_stops_at_the_since_cutoff() -> None:
    """The feed is newest first, so the first older row ends the walk."""
    now = datetime.now(UTC)
    client, _ = _client(
        [
            {
                "activities": [
                    {"id": "new", "createTime": int(now.timestamp() * 1000)},
                    {
                        "id": "old",
                        "createTime": int((now - timedelta(days=30)).timestamp() * 1000),
                    },
                ],
                "eof": True,
            }
        ]
    )

    trades = client.trades(since=now - timedelta(days=1))

    assert [t["id"] for t in trades] == ["new"]


def test_a_non_list_activities_payload_raises_rather_than_reading_as_empty() -> None:
    """Returning nothing would read as 'you have placed no bets', which is a lie."""
    client, _ = _client([{"activities": "nope"}])

    with pytest.raises(PolymarketUsAuthError, match="expected a list"):
        client.trades()


def test_a_transport_failure_never_echoes_the_signature() -> None:
    class Boom:
        def get_json(self, *args, **kwargs):
            raise RuntimeError("401 unauthorized")

        def post_json(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError

    client = PolymarketUsPrivateClient(Boom(), KEY_ID, SECRET_B64)
    with pytest.raises(PolymarketUsAuthError) as caught:
        client.trades()

    message = str(caught.value)
    assert "fetch failed" in message and "401" in message
    assert SECRET_B64 not in message and "X-PM-Signature" not in message

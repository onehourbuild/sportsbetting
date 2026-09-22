"""Polymarket US authenticated reads: the owner's own fills.

Separate from `polymarket_us` because the security properties differ. That module talks to
public endpoints and holds nothing; this one holds an Ed25519 private key.

**It reads. It does not trade.** There is no code here that places, cancels, modifies or
sizes an order, and there never should be: the app's whole contract is that the owner
trades by hand and the app keeps score. The credential exists so the ledger can be filled
from the exchange's own record of what the owner already did, rather than from memory.

The credential is created by the account holder at polymarket.us/developer after identity
verification, and the secret is shown once. It belongs in `.env`, never in source, and
nobody but the owner should ever generate one. Both halves are `SecretStr` in
`app.settings`, so a stray `repr()` of the settings object cannot spill them into a log --
a failure this project has already had once, with the Odds API key.

Signing, per the notes taken off the live API on 2026-09-21 (docs/RESEARCH.md):

    X-PM-Access-Key   the key id (a UUID)
    X-PM-Timestamp    milliseconds since the epoch, within 30s of server time
    X-PM-Signature    base64(Ed25519(timestamp + method + path))

One thing here cannot be settled without a live 401: whether `path` includes the query
string. This module signs the path alone and says so, because that is what the note
records. If the live API disagrees the request fails authentication loudly, which is the
right way for an unverified assumption to fail -- unlike a mis-parsed price, a bad
signature cannot quietly cost money.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.clients.transport import Transport

log = logging.getLogger(__name__)

API = "https://api.polymarket.us"
ACTIVITIES_PATH = "/v1/portfolio/activities"
TRADE_TYPE = "ACTIVITY_TYPE_TRADE"
PAGE_LIMIT = 100
MAX_PAGES = 100

# Ed25519 keys are 32 bytes. Some issuers hand out the 64-byte "seed + public key" form,
# where the seed is the first half.
SEED_BYTES = 32
EXPANDED_BYTES = 64


class PolymarketUsAuthError(Exception):
    """The credential is unusable, or the API rejected it."""


def load_signing_key(secret: str) -> Ed25519PrivateKey:
    """Accept the shapes an Ed25519 secret is handed out in, and reject anything else.

    Tried in turn: PEM, base64 (32-byte seed or 64-byte expanded), hex. The error names
    what was wrong with the length rather than echoing the value, because the value is the
    credential.
    """
    text = (secret or "").strip()
    if not text:
        raise PolymarketUsAuthError("no Polymarket US API secret configured")

    if "-----BEGIN" in text:
        try:
            key = serialization.load_pem_private_key(text.encode(), password=None)
        except (ValueError, TypeError, InvalidKey) as exc:
            raise PolymarketUsAuthError(f"PEM secret could not be read: {exc}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise PolymarketUsAuthError(f"PEM secret is {type(key).__name__}, not Ed25519")
        return key

    # Decode by every reading that works, then prefer whichever lands on a key length.
    # A 64-character hex key is also valid base64, decoding to 48 meaningless bytes, so
    # picking the first successful decode rather than the plausible one would reject a
    # perfectly good hex secret.
    decoded = [raw for raw in (_from_base64(text), _from_hex(text)) if raw is not None]
    if not decoded:
        raise PolymarketUsAuthError("API secret is neither PEM, base64 nor hex")
    raw = next(
        (candidate for candidate in decoded if len(candidate) in (SEED_BYTES, EXPANDED_BYTES)),
        decoded[0],
    )

    if len(raw) == EXPANDED_BYTES:
        raw = raw[:SEED_BYTES]
    if len(raw) != SEED_BYTES:
        raise PolymarketUsAuthError(
            f"API secret decodes to {len(raw)} bytes; an Ed25519 key is "
            f"{SEED_BYTES} (or {EXPANDED_BYTES} with the public half appended)"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def _from_base64(text: str) -> bytes | None:
    """Standard or URL-safe base64, of the right length to be a key.

    The two alphabets differ only in `-_` versus `+/`, so normalise and decode once --
    `urlsafe_b64decode` takes no `validate=`, and without validation a hex string decodes
    happily into the wrong number of bytes instead of being handed on to the hex reader.
    """
    normalised = text.replace("-", "+").replace("_", "/")
    padded = normalised + "=" * (-len(normalised) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _from_hex(text: str) -> bytes | None:
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def signing_payload(timestamp_ms: int, method: str, path: str) -> bytes:
    """`timestamp + method + path`, concatenated. Method upper-cased, path as given."""
    return f"{timestamp_ms}{method.upper()}{path}".encode()


def signature_for(key: Ed25519PrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    return base64.b64encode(key.sign(signing_payload(timestamp_ms, method, path))).decode()


def auth_headers(
    key_id: str, key: Ed25519PrivateKey, method: str, path: str, *, timestamp_ms: int | None = None
) -> dict[str, str]:
    """The three headers a .us private request needs.

    The timestamp must be within 30s of server time, so it is generated per request and
    never cached alongside the signature it is bound to.
    """
    stamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": str(stamp),
        "X-PM-Signature": signature_for(key, stamp, method, path),
    }


class PolymarketUsPrivateClient:
    """Reads the owner's own activity feed. Read-only by construction."""

    def __init__(
        self,
        transport: Transport,
        key_id: str,
        secret: str,
        api_base: str = API,
    ) -> None:
        if not (key_id or "").strip():
            raise PolymarketUsAuthError("no Polymarket US API key id configured")
        self.transport = transport
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id.strip()
        self._key = load_signing_key(secret)

    def trades(self, *, since: datetime | None = None, limit: int = PAGE_LIMIT) -> list[dict]:
        """Every trade in the activity feed, newest first, stopping once older than `since`."""
        collected: list[dict] = []
        for activity in self._iter_activities(limit):
            created = _created_at(activity)
            if since is not None and created is not None and created < since:
                break
            collected.append(activity)
        return collected

    def _iter_activities(self, limit: int) -> Iterator[dict]:
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "limit": limit,
                "types": TRADE_TYPE,
                "sortOrder": "SORT_ORDER_DESC",
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._get(ACTIVITIES_PATH, params)
            activities = payload.get("activities") if isinstance(payload, dict) else None
            if not isinstance(activities, list):
                raise PolymarketUsAuthError(
                    f"activities: expected a list, got {type(activities).__name__}"
                )
            for activity in activities:
                if isinstance(activity, dict):
                    yield activity
            if not isinstance(payload, dict) or payload.get("eof") or not payload.get("nextCursor"):
                return
            cursor = str(payload["nextCursor"])

    def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        headers = auth_headers(self.key_id, self._key, "GET", path)
        try:
            payload, _ = self.transport.get_json(
                f"{self.api_base}{path}", params=params, headers=headers
            )
        except Exception as exc:
            # Never interpolate headers or params into this: one carries the signature.
            raise PolymarketUsAuthError(f".us {path} fetch failed: {exc}") from exc
        return payload


def _created_at(activity: Mapping[str, Any]) -> datetime | None:
    raw = activity.get("createTime")
    if raw is None:
        return None
    if isinstance(raw, int | float):
        # Milliseconds if it is far too large to be seconds.
        seconds = raw / 1000 if raw > 1e11 else raw
        return datetime.fromtimestamp(seconds, tz=UTC)
    from app.clients.polymarket import parse_iso_utc

    return parse_iso_utc(raw)


__all__ = [
    "ACTIVITIES_PATH",
    "API",
    "TRADE_TYPE",
    "PolymarketUsAuthError",
    "PolymarketUsPrivateClient",
    "auth_headers",
    "load_signing_key",
    "signature_for",
    "signing_payload",
]

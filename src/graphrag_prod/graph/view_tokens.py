"""Short-lived, purpose-separated HMAC graph view/cursor tokens.

Tokens confer no data permissions. Every use rechecks the current principal,
source ACLs and graph state. Payloads contain only previously authorized view
metadata, never whole publication manifests or raw source text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Callable

from graphrag_prod.domain.access import Principal

from .browse_models import GraphViewChanged


MAX_GRAPH_TOKEN_CHARS = 65_536
MAX_GRAPH_TOKEN_JSON_BYTES = 48_000


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate token field")
        result[key] = value
    return result


class GraphViewTokenCodec:
    def __init__(self, signing_key: bytes | None = None, *, clock: Callable[[], float] = time.time, lifetime_seconds: int = 600) -> None:
        key = secrets.token_bytes(32) if signing_key is None else signing_key
        if not isinstance(key, bytes) or not 32 <= len(key) <= 4096:
            raise ValueError("graph token signing key must contain 32–4096 bytes")
        if type(lifetime_seconds) is not int or not 30 <= lifetime_seconds <= 3600 or not callable(clock):
            raise ValueError("graph token clock/lifetime is invalid")
        self._key = key
        self._clock = clock
        self._lifetime = lifetime_seconds

    def _principal(self, principal: Principal) -> str:
        return hmac.new(self._key, canonical_json([
            "graph-principal:v1", principal.tenant_id, principal.principal_id,
            sorted(principal.groups), sorted(principal.capabilities),
        ]).encode(), hashlib.sha256).hexdigest()

    def encode(self, principal: Principal, purpose: str, payload: dict[str, Any], *, expires_at: int | None = None) -> str:
        if purpose not in {"view", "cursor"}:
            raise ValueError("invalid graph token purpose")
        expires = int(self._clock()) + self._lifetime if expires_at is None else expires_at
        envelope = {"v": 1, "purpose": purpose, "principal": self._principal(principal), "expires": expires, "payload": payload}
        raw = canonical_json(envelope).encode("utf-8")
        if len(raw) > MAX_GRAPH_TOKEN_JSON_BYTES:
            raise ValueError("graph token exceeds its byte bound")
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
        signature = hmac.new(self._key, b"graph-token:v1:" + encoded, hashlib.sha256).hexdigest().encode()
        return (encoded + b"." + signature).decode("ascii")

    def decode(self, principal: Principal, purpose: str, token: str) -> tuple[dict[str, Any], int]:
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= MAX_GRAPH_TOKEN_CHARS:
                raise ValueError("invalid token size")
            encoded, signature = token.encode("ascii").split(b".")
            expected = hmac.new(self._key, b"graph-token:v1:" + encoded, hashlib.sha256).hexdigest().encode()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("invalid token signature")
            raw = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if len(raw) > MAX_GRAPH_TOKEN_JSON_BYTES:
                raise ValueError("invalid token size")
            envelope = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(envelope, dict) or set(envelope) != {"v", "purpose", "principal", "expires", "payload"} or envelope["v"] != 1 or type(envelope["v"]) is not int or envelope["purpose"] != purpose or not hmac.compare_digest(str(envelope["principal"]), self._principal(principal)):
                raise ValueError("invalid token binding")
            if type(envelope["expires"]) is not int or not int(self._clock()) < envelope["expires"] <= int(self._clock()) + self._lifetime or not isinstance(envelope["payload"], dict):
                raise ValueError("invalid token lifetime/payload")
            return envelope["payload"], envelope["expires"]
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise GraphViewChanged() from error

"""Fail-closed JWT authentication for the HTTP boundary.

The verifier deliberately supports one deployment-controlled algorithm.  JWT
headers are untrusted input and never select the verification algorithm or
key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Final

import jwt

from graphrag_prod.domain.access import Principal


_ALGORITHM: Final = "HS256"
_AUTHENTICATION_FAILED: Final = "authentication failed"
_MIN_SECRET_BYTES: Final = 32
_MAX_SECRET_BYTES: Final = 4_096
_MAX_IDENTITY_CHARS: Final = 256
_MAX_GROUP_CHARS: Final = 128
_MAX_GROUPS: Final = 64
_MAX_SCOPE_CHARS: Final = 128
_MAX_SCOPES: Final = 64
_IDENTITY_PATTERN: Final = re.compile(r"^[^\x00-\x20\x7f]{1,256}$")
_GROUP_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_SCOPE_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")


class AuthenticationError(ValueError):
    """A public, deliberately non-diagnostic authentication failure."""


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    """Verified data used at both data- and action-authorization boundaries.

    Access groups remain part of the domain :class:`Principal` and restrict
    which source records may be recalled.  OAuth-style scopes are kept
    separate so a data group can never implicitly grant an API action such as
    document ingestion or deletion.
    """

    principal: Principal
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be Principal")
        if not isinstance(self.scopes, frozenset) or not self.scopes:
            raise TypeError("scopes must be a non-empty frozenset")
        if any(not isinstance(scope, str) for scope in self.scopes):
            raise TypeError("scopes must contain only text")


def _configuration_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > _MAX_IDENTITY_CHARS or "\x00" in normalized:
        raise ValueError(f"{name} is invalid")
    return normalized


def _secret_bytes(value: object) -> bytes:
    if isinstance(value, str):
        if value != value.strip() or "\x00" in value:
            raise ValueError("JWT secret must not contain surrounding whitespace or NUL")
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value
    else:
        raise TypeError("JWT secret must be text or bytes")
    if not _MIN_SECRET_BYTES <= len(encoded) <= _MAX_SECRET_BYTES:
        raise ValueError("JWT secret must contain between 32 and 4096 bytes")
    # Length alone does not make values such as ``aaaa...`` suitable MAC keys.
    if len(set(encoded)) < 8:
        raise ValueError("JWT secret has insufficient byte diversity")
    return encoded


@dataclass(frozen=True, slots=True)
class JWTAuthConfig:
    """Deployment-owned JWT verification policy.

    ``max_lifetime_seconds`` limits damage from a mistakenly issued long-lived
    bearer token.  It is independent from the small clock-skew allowance.
    """

    issuer: str
    audience: str
    secret: str | bytes = field(repr=False)
    leeway_seconds: int = 30
    max_token_bytes: int = 8_192
    max_lifetime_seconds: int = 3_600

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _configuration_text(self.issuer, "issuer"))
        object.__setattr__(
            self,
            "audience",
            _configuration_text(self.audience, "audience"),
        )
        object.__setattr__(self, "secret", _secret_bytes(self.secret))
        for name, minimum, maximum in (
            ("leeway_seconds", 0, 120),
            ("max_token_bytes", 256, 65_536),
            ("max_lifetime_seconds", 60, 86_400),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _claim_text(value: object) -> str:
    if not isinstance(value, str):
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > _MAX_IDENTITY_CHARS
        or _IDENTITY_PATTERN.fullmatch(normalized) is None
    ):
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    return normalized


def _claim_groups(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_GROUPS:
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    groups: list[str] = []
    for item in value:
        group = _claim_text(item)
        if len(group) > _MAX_GROUP_CHARS or _GROUP_PATTERN.fullmatch(group) is None:
            raise AuthenticationError(_AUTHENTICATION_FAILED)
        groups.append(group)
    if len(groups) != len(set(groups)):
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    return frozenset(groups)


def _claim_scopes(value: object) -> frozenset[str]:
    """Parse one canonical, ASCII-space-delimited JWT ``scope`` claim."""
    if not isinstance(value, str) or not value or value != value.strip(" "):
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    # Only a single ASCII SP may delimit tokens.  This rejects tabs, line
    # breaks, repeated spaces, and other Unicode whitespace before any
    # authorization comparison can normalize them inconsistently.
    raw_scopes = value.split(" ")
    if not 1 <= len(raw_scopes) <= _MAX_SCOPES or any(not item for item in raw_scopes):
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    scopes: list[str] = []
    for scope in raw_scopes:
        if len(scope) > _MAX_SCOPE_CHARS or _SCOPE_PATTERN.fullmatch(scope) is None:
            raise AuthenticationError(_AUTHENTICATION_FAILED)
        scopes.append(scope)
    if len(scopes) != len(set(scopes)):
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    return frozenset(scopes)


class JWTAuthenticator:
    """Verify an HS256 bearer token and return the shared domain Principal."""

    def __init__(self, config: JWTAuthConfig) -> None:
        if not isinstance(config, JWTAuthConfig):
            raise TypeError("config must be JWTAuthConfig")
        self._config = config

    def verify_identity(self, token: str) -> AuthenticatedIdentity:
        """Verify signature, registered claims, and both authorization layers.

        All token-dependent failures intentionally collapse to one message so
        callers cannot turn the API into a token or claim oracle.
        """
        try:
            if not isinstance(token, str):
                raise AuthenticationError(_AUTHENTICATION_FAILED)
            if not token or token != token.strip() or "\x00" in token:
                raise AuthenticationError(_AUTHENTICATION_FAILED)
            try:
                encoded = token.encode("ascii")
            except UnicodeEncodeError as error:
                raise AuthenticationError(_AUTHENTICATION_FAILED) from error
            if len(encoded) > self._config.max_token_bytes:
                raise AuthenticationError(_AUTHENTICATION_FAILED)

            header = jwt.get_unverified_header(token)
            # This check is defense in depth only.  ``algorithms`` below is a
            # literal fixed allow-list and never derives from this header.
            if header.get("alg") != _ALGORITHM:
                raise AuthenticationError(_AUTHENTICATION_FAILED)
            if "crit" in header or header.get("b64") is False:
                raise AuthenticationError(_AUTHENTICATION_FAILED)
            token_type = header.get("typ")
            if token_type is not None and token_type != "JWT":
                raise AuthenticationError(_AUTHENTICATION_FAILED)

            claims = jwt.decode(
                token,
                self._config.secret,
                algorithms=[_ALGORITHM],
                issuer=self._config.issuer,
                audience=self._config.audience,
                leeway=self._config.leeway_seconds,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "exp",
                        "iat",
                        "sub",
                        "tenant_id",
                        "groups",
                        "scope",
                    ],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_sub": True,
                    "strict_aud": True,
                },
            )

            issued_at = claims.get("iat")
            expires_at = claims.get("exp")
            if (
                isinstance(issued_at, bool)
                or not isinstance(issued_at, int)
                or isinstance(expires_at, bool)
                or not isinstance(expires_at, int)
                or expires_at <= issued_at
                or expires_at - issued_at > self._config.max_lifetime_seconds
            ):
                raise AuthenticationError(_AUTHENTICATION_FAILED)

            return AuthenticatedIdentity(
                principal=Principal(
                    principal_id=_claim_text(claims.get("sub")),
                    tenant_id=_claim_text(claims.get("tenant_id")),
                    groups=_claim_groups(claims.get("groups")),
                ),
                scopes=_claim_scopes(claims.get("scope")),
            )
        except AuthenticationError:
            raise
        except Exception as error:
            # Never expose a token, decoded claim, signature detail, or PyJWT
            # exception text through the public exception.
            raise AuthenticationError(_AUTHENTICATION_FAILED) from error

    def verify(self, token: str) -> Principal:
        """Compatibility facade returning the verified domain principal.

        The scope claim is still required and validated.  New HTTP boundary
        code should use :meth:`verify_identity` so it can enforce action-level
        authorization independently from record access groups.
        """
        return self.verify_identity(token).principal


def extract_bearer_token(authorization_header: str | None) -> str:
    """Extract one RFC 6750-style bearer credential without logging it."""
    if not isinstance(authorization_header, str):
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    parts = authorization_header.split()
    if len(parts) != 2 or parts[0].casefold() != "bearer" or not parts[1]:
        raise AuthenticationError(_AUTHENTICATION_FAILED)
    return parts[1]


# Short aliases make dependency wiring readable while retaining explicit public
# names in documentation and tests.
JWTConfig = JWTAuthConfig
JWTVerifier = JWTAuthenticator


__all__ = [
    "AuthenticatedIdentity",
    "AuthenticationError",
    "JWTAuthConfig",
    "JWTAuthenticator",
    "JWTConfig",
    "JWTVerifier",
    "extract_bearer_token",
]

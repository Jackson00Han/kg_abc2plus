"""Security tests for the Stage 7 JWT authentication boundary."""

from __future__ import annotations

from datetime import UTC, datetime
import unittest

import jwt

from graphrag_prod.api.auth import (
    AuthenticatedIdentity,
    AuthenticationError,
    JWTAuthConfig,
    JWTAuthenticator,
    extract_bearer_token,
)
from graphrag_prod.domain import Principal


SECRET = "stage7-fixture-key-3Rr!6pQ9xV2mN8cL5sT1"
ISSUER = "https://identity.example.test"
AUDIENCE = "graphrag-api"


def _claims(**changes: object) -> dict[str, object]:
    now = int(datetime.now(UTC).timestamp())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": now + 600,
        "iat": now,
        "sub": "reader-1",
        "tenant_id": "tenant-alpha",
        "groups": ["finance-readers", "public"],
        "scope": "retrieval:read answers:generate",
    }
    claims.update(changes)
    return claims


def _token(
    claims: dict[str, object] | None = None,
    *,
    key: str = SECRET,
    algorithm: str = "HS256",
) -> str:
    return jwt.encode(claims or _claims(), key, algorithm=algorithm)


class JWTAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authenticator = JWTAuthenticator(
            JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
        )

    def test_valid_token_returns_shared_domain_principal(self) -> None:
        principal = self.authenticator.verify(_token())
        self.assertIsInstance(principal, Principal)
        self.assertEqual(principal.principal_id, "reader-1")
        self.assertEqual(principal.tenant_id, "tenant-alpha")
        self.assertEqual(principal.groups, frozenset({"finance-readers", "public"}))

    def test_valid_token_returns_identity_with_separate_action_scopes(self) -> None:
        identity = self.authenticator.verify_identity(_token())
        self.assertIsInstance(identity, AuthenticatedIdentity)
        self.assertEqual(identity.principal.principal_id, "reader-1")
        self.assertEqual(
            identity.principal.groups,
            frozenset({"finance-readers", "public"}),
        )
        self.assertEqual(
            identity.scopes,
            frozenset({"retrieval:read", "answers:generate"}),
        )

    def test_scope_claim_is_required_canonical_and_bounded(self) -> None:
        missing_scope = _claims()
        del missing_scope["scope"]
        invalid_scope_claims: tuple[object, ...] = (
            None,
            [],
            "",
            " retrieval:read",
            "retrieval:read ",
            "retrieval:read  answers:generate",
            "retrieval:read\tanswers:generate",
            "retrieval:read\nanswers:generate",
            "retrieval:read retrieval:read",
            "retrieval:read?tenant=other",
            "x" * 129,
            " ".join(f"scope:{index}" for index in range(65)),
        )
        invalid_claim_sets = (missing_scope,) + tuple(
            _claims(scope=value) for value in invalid_scope_claims
        )
        for claims in invalid_claim_sets:
            with self.subTest(scope=claims.get("scope")):
                with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
                    self.authenticator.verify_identity(_token(claims))

    def test_verify_compatibility_still_validates_scope(self) -> None:
        with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
            self.authenticator.verify(_token(_claims(scope="retrieval:read retrieval:read")))

    def test_algorithm_is_fixed_and_never_selected_from_header(self) -> None:
        for forged in (
            jwt.encode(
                _claims(),
                f"{SECRET}-separate-hs512-key-with-at-least-sixty-four-bytes",
                algorithm="HS512",
            ),
            jwt.encode(_claims(), key="", algorithm="none"),
        ):
            with self.subTest(header=jwt.get_unverified_header(forged)):
                with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
                    self.authenticator.verify(forged)

    def test_wrong_signature_expiry_issuer_and_audience_fail_closed(self) -> None:
        now = int(datetime.now(UTC).timestamp())
        bad_tokens = (
            _token(key="different-fixture-key-7Yu!2xP0vN4kM9cQ6zL3"),
            _token(_claims(iat=now - 601, exp=now - 31)),
            _token(_claims(iss="https://attacker.example")),
            _token(_claims(aud="different-api")),
            _token(_claims(aud=[AUDIENCE])),
        )
        for token in bad_tokens:
            with self.subTest(token_kind=jwt.get_unverified_header(token)["alg"]):
                with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
                    self.authenticator.verify(token)

    def test_required_claims_and_claim_types_are_strict(self) -> None:
        missing_sub = _claims()
        del missing_sub["sub"]
        invalid_claim_sets = (
            missing_sub,
            _claims(sub=7),
            _claims(tenant_id=" tenant-alpha"),
            _claims(groups="finance-readers"),
            _claims(groups=[]),
            _claims(groups=["public", "public"]),
            _claims(groups=["public\nforged"]),
            _claims(iat=True),
            _claims(exp="9999999999"),
        )
        for claims in invalid_claim_sets:
            with self.subTest(claims=claims):
                with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
                    self.authenticator.verify(_token(claims))

    def test_future_iat_and_excessive_lifetime_are_rejected(self) -> None:
        now = int(datetime.now(UTC).timestamp())
        within_leeway = self.authenticator.verify(
            _token(_claims(iat=now + 20, exp=now + 300))
        )
        self.assertEqual(within_leeway.principal_id, "reader-1")
        tokens = (
            _token(_claims(iat=now + 60, exp=now + 300)),
            _token(_claims(iat=now, exp=now + 3_601)),
        )
        for token in tokens:
            with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
                self.authenticator.verify(token)

    def test_token_size_type_and_header_extensions_are_rejected(self) -> None:
        small_verifier = JWTAuthenticator(
            JWTAuthConfig(
                issuer=ISSUER,
                audience=AUDIENCE,
                secret=SECRET,
                max_token_bytes=256,
            )
        )
        with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
            small_verifier.verify(_token(_claims(extra="x" * 500)))
        with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
            self.authenticator.verify(b"not-text")  # type: ignore[arg-type]

        critical = jwt.encode(
            _claims(),
            SECRET,
            algorithm="HS256",
            headers={"crit": ["unknown"], "unknown": True},
        )
        with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
            self.authenticator.verify(critical)

    def test_public_error_does_not_echo_token_or_decoded_identity(self) -> None:
        token = _token(_claims(sub="protected-subject"), key="different-fixture-key-7Yu!2xP0vN4kM9cQ6zL3")
        with self.assertRaises(AuthenticationError) as captured:
            self.authenticator.verify(token)
        rendered = str(captured.exception)
        self.assertEqual(rendered, "authentication failed")
        self.assertNotIn(token, rendered)
        self.assertNotIn("protected-subject", rendered)

    def test_configuration_requires_a_strong_bounded_key_and_valid_bounds(self) -> None:
        valid = JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
        self.assertNotIn(SECRET, repr(valid))
        invalid_configs = (
            {"secret": "short"},
            {"secret": "a" * 32},
            {"secret": 123},
            {"secret": SECRET, "leeway_seconds": True},
            {"secret": SECRET, "leeway_seconds": 121},
            {"secret": SECRET, "max_lifetime_seconds": 30},
        )
        for changes in invalid_configs:
            with self.subTest(changes=changes):
                with self.assertRaises((TypeError, ValueError)):
                    JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, **changes)

    def test_bearer_header_requires_exactly_one_credential(self) -> None:
        self.assertEqual(extract_bearer_token("Bearer abc.def.ghi"), "abc.def.ghi")
        self.assertEqual(extract_bearer_token("bearer abc.def.ghi"), "abc.def.ghi")
        for header in (None, "", "Basic value", "Bearer", "Bearer one two"):
            with self.subTest(header=header):
                with self.assertRaisesRegex(AuthenticationError, "^authentication failed$"):
                    extract_bearer_token(header)


if __name__ == "__main__":
    unittest.main()

"""Verifying a Supabase access token.

Lives in ``infra`` because it is vendor-shaped: the issuer, the audience and the
key discovery URL are all Supabase's. ``api/deps.py`` wires it; nothing in
``domain`` knows it exists.

**This code fails closed or it is worthless.** An authentication check that
accepts a malformed token, an unsigned one, or one signed with the wrong key is
worse than no check at all — it reports success and admits everyone, and the
tests that matter here are the ones asserting rejection, not acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from jwt import PyJWKClient

from app.core.errors import AuthenticationError, ConfigurationError

# Supabase sets `aud` to `authenticated` for a signed-in user's access token.
# Verified rather than ignored: a token minted for a different audience is a
# token for a different system.
AUDIENCE = "authenticated"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The parts of a verified token this application uses."""

    subject: UUID
    email: str | None


class SupabaseTokenVerifier:
    """Verifies access tokens against a Supabase project.

    Supports both signing schemes because projects differ: newer ones sign
    asymmetrically and publish a JWKS, older ones use a shared HS256 secret. The
    JWKS is preferred when configured — a public key that can be rotated without
    redeploying beats a shared secret that cannot.
    """

    def __init__(self, *, jwks_url: str | None = None, secret: str | None = None) -> None:
        if not jwks_url and not secret:
            # Raised at construction rather than per request: a verifier with no
            # key cannot fail closed on a per-request basis without silently
            # rejecting everybody, and an operator needs to know at wiring time.
            raise ConfigurationError(
                "Set EDUFURTHER_SUPABASE_JWKS_URL or EDUFURTHER_SUPABASE_JWT_SECRET. "
                "Both are under Settings -> API -> JWT Keys in the Supabase dashboard."
            )
        self._secret = secret
        # `PyJWKClient` caches the key set, so this is one fetch per key rotation
        # rather than one per request.
        self._jwks = PyJWKClient(jwks_url) if jwks_url else None

    def verify(self, token: str) -> TokenClaims:
        """Return the claims of a valid token, or raise.

        Every failure mode collapses to ``InvalidTokenError``. The library's own
        exception types are informative and are exactly what must not reach a
        caller.
        """
        try:
            payload = self._decode(token)
        except (jwt.PyJWTError, ValueError) as exc:
            raise AuthenticationError("token is not valid") from exc

        subject = payload.get("sub")
        if not subject:
            raise AuthenticationError("token carries no subject")

        try:
            return TokenClaims(subject=UUID(str(subject)), email=payload.get("email"))
        except ValueError as exc:
            # A `sub` that is not a uuid cannot match `users.auth_id`, and
            # letting it through would turn an auth failure into a database
            # error somewhere less obvious.
            raise AuthenticationError("token subject is not a uuid") from exc

    def _decode(self, token: str) -> dict[str, Any]:
        if self._jwks is not None:
            key = self._jwks.get_signing_key_from_jwt(token).key
            # Algorithms are named explicitly. Passing the header's `alg` back to
            # the verifier is the classic JWT confusion attack: a token claiming
            # `alg: none`, or an RS256 public key used as an HS256 shared secret.
            return dict(jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=AUDIENCE))

        assert self._secret is not None  # noqa: S101 — guaranteed by __init__
        return dict(jwt.decode(token, self._secret, algorithms=["HS256"], audience=AUDIENCE))

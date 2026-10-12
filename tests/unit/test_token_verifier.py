"""The token checks added for production: issuer, a required expiry, and a
verification that never runs on the event loop."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr

from app.api.deps import core as deps_core
from app.api.deps.core import get_claims
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.infra.auth.dev_tokens import mint_dev_token
from app.infra.auth.supabase import (
    AUDIENCE,
    JWKS_TIMEOUT,
    SYMMETRIC_ALGORITHM,
    SupabaseTokenVerifier,
    TokenClaims,
    issuer_for,
)

SECRET = "unit-test-signing-secret-of-enough-length"  # noqa: S105
ISSUER = "https://abcdefghijklmnopqrst.supabase.co/auth/v1"


def test_the_issuer_is_the_projects_auth_url() -> None:
    assert issuer_for("https://abcdefghijklmnopqrst.supabase.co/") == ISSUER
    assert issuer_for(" https://abcdefghijklmnopqrst.supabase.co\t") == ISSUER
    assert issuer_for(None) is None
    assert issuer_for("  ") is None


def test_the_app_verifier_checks_the_configured_projects_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring production uses; the suite's own verifiers name no issuer."""
    configured = Settings(
        _env_file=None,
        supabase_url="https://abcdefghijklmnopqrst.supabase.co",
        supabase_jwt_secret=SecretStr(SECRET),
    )
    monkeypatch.setattr(deps_core, "get_settings", lambda: configured)
    deps_core.get_verifier.cache_clear()
    try:
        verifier = deps_core.get_verifier()
        assert verifier.verify(mint_dev_token(uuid4(), secret=SECRET, issuer=ISSUER))
        with pytest.raises(AuthenticationError):
            verifier.verify(mint_dev_token(uuid4(), secret=SECRET, issuer="https://x/auth/v1"))
    finally:
        deps_core.get_verifier.cache_clear()


def test_a_token_from_this_project_is_accepted() -> None:
    subject = uuid4()
    token = mint_dev_token(subject, secret=SECRET, issuer=ISSUER)

    claims = SupabaseTokenVerifier(secret=SECRET, issuer=ISSUER).verify(token)

    assert claims.subject == subject


@pytest.mark.parametrize("issuer", ["https://another.supabase.co/auth/v1", "supabase", None])
def test_a_token_from_anywhere_else_is_refused(issuer: str | None) -> None:
    """Another project's token, Supabase's anon/service keys (`iss: supabase`),
    and a token naming no issuer at all."""
    token = mint_dev_token(uuid4(), secret=SECRET, issuer=issuer)

    with pytest.raises(AuthenticationError):
        SupabaseTokenVerifier(secret=SECRET, issuer=ISSUER).verify(token)


def test_with_no_project_named_the_issuer_is_not_checked() -> None:
    """Local development and the suite, which name no Supabase project."""
    token = mint_dev_token(uuid4(), secret=SECRET, issuer="https://anything.example/auth/v1")

    assert SupabaseTokenVerifier(secret=SECRET).verify(token)


def test_a_token_with_no_expiry_is_refused() -> None:
    """Without `require`, PyJWT accepts a token that never expires."""
    claims = {"sub": str(uuid4()), "aud": AUDIENCE, "iss": ISSUER}
    token = jwt.encode(claims, SECRET, algorithm=SYMMETRIC_ALGORITHM)

    with pytest.raises(AuthenticationError):
        SupabaseTokenVerifier(secret=SECRET, issuer=ISSUER).verify(token)


def test_an_expired_token_is_still_refused() -> None:
    expired = datetime.now(UTC) - timedelta(minutes=1)
    token = mint_dev_token(uuid4(), secret=SECRET, issuer=ISSUER, exp=expired)

    with pytest.raises(AuthenticationError):
        SupabaseTokenVerifier(secret=SECRET, issuer=ISSUER).verify(token)


def test_the_key_set_fetch_gives_up_well_before_pyjwts_thirty_seconds() -> None:
    verifier = SupabaseTokenVerifier(jwks_url="https://project.example/jwks.json")

    assert verifier._jwks is not None
    assert verifier._jwks.timeout == JWKS_TIMEOUT == 10


@pytest.mark.asyncio
async def test_verification_runs_off_the_event_loop() -> None:
    """A key-set refetch blocks; on the loop it would stall every request."""
    loop_thread = threading.get_ident()
    seen: list[int] = []

    class Recording:
        def verify(self, _token: str) -> TokenClaims:
            seen.append(threading.get_ident())
            return TokenClaims(subject=uuid4(), email=None)

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(token_verifier=Recording()))
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="t")

    await get_claims(request, credentials)  # type: ignore[arg-type]

    assert seen and seen[0] != loop_thread
    assert asyncio.get_running_loop()

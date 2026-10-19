"""Signing a deleted account out everywhere (`DELETE /me`): the Supabase call.

Supabase's admin `signOut(jwt, scope)` is `POST /auth/v1/logout?scope=global`
authorised by the **user's** access token; the service key only routes it.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.infra.auth.admin import AdminApiError, SupabaseAdminClient, auth_admin_for

KEY = "service-role-key"
TOKEN = "user.access.token"  # noqa: S105 - a fake bearer token


def client(status: int, seen: list[httpx.Request]) -> SupabaseAdminClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return SupabaseAdminClient(
        base_url="https://project.supabase.co/",
        service_role_key=KEY,
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        sleep=lambda _: None,
    )


def test_sign_out_revokes_every_session_with_the_users_own_token() -> None:
    seen: list[httpx.Request] = []

    client(204, seen).sign_out_everywhere(TOKEN)

    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == "https://project.supabase.co/auth/v1/logout?scope=global"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["apikey"] == KEY


def test_a_refused_sign_out_raises_without_the_token() -> None:
    with pytest.raises(AdminApiError) as raised:
        client(401, []).sign_out_everywhere(TOKEN)

    assert "401" in str(raised.value)
    assert TOKEN not in str(raised.value) and KEY not in str(raised.value)


def test_no_admin_client_without_supabase_configured() -> None:
    assert auth_admin_for(Settings(_env_file=None, supabase_url=None)) is None
    configured = Settings(
        _env_file=None,
        supabase_url="https://project.supabase.co",
        supabase_service_role_key=SecretStr(KEY),
    )
    assert isinstance(auth_admin_for(configured), SupabaseAdminClient)

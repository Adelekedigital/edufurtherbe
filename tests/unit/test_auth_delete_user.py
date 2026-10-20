"""Removing a purged account's Supabase sign-in, and the job refusing to purge
without the Admin API (Mentee Settings PRD, purge)."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.infra.auth.admin import AdminApiError, SupabaseAdminClient
from app.infra.clients.loops_contacts import ContactsError, LoopsContacts
from app.infra.jobs.runner import RuntimeJobs

KEY = "service-role-key"


def client(handle: object) -> SupabaseAdminClient:
    return SupabaseAdminClient(
        base_url="https://project.supabase.co",
        service_role_key=KEY,
        client=httpx.Client(transport=httpx.MockTransport(handle)),  # type: ignore[arg-type]
        sleep=lambda _: None,
    )


def answering(status: int, seen: list[httpx.Request]) -> object:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return handle


def test_delete_user_hard_deletes_with_the_service_key() -> None:
    seen: list[httpx.Request] = []
    auth_id = uuid4()

    client(answering(200, seen)).delete_user(auth_id)

    [request] = seen
    assert request.method == "DELETE"
    assert str(request.url) == f"https://project.supabase.co/auth/v1/admin/users/{auth_id}"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.content == b""  # no `should_soft_delete`: Supabase's hard delete


def test_a_sign_in_already_gone_counts_as_deleted() -> None:
    client(answering(404, [])).delete_user(uuid4())


@pytest.mark.parametrize("status", [401, 500])
def test_a_refusal_raises_without_the_key(status: int) -> None:
    with pytest.raises(AdminApiError) as raised:
        client(answering(status, [])).delete_user(uuid4())

    assert str(status) in str(raised.value) and KEY not in str(raised.value)


def test_an_unreachable_supabase_raises() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AdminApiError, match="ConnectError"):
        client(refuse).delete_user(uuid4())


@pytest.mark.asyncio
async def test_the_job_purges_nothing_without_the_admin_api() -> None:
    """Purging our row while Supabase keeps the sign-in would orphan it."""
    jobs = RuntimeJobs(Settings(_env_file=None, supabase_url=None))

    result = await jobs.run("purge-deleted-users")

    assert result.counts == {"due": 0, "purged": 0, "failed": 0}
    assert result.status == "no-op"


def loops(status: int, seen: list[httpx.Request]) -> LoopsContacts:
    return LoopsContacts(
        "loops-key", httpx.Client(transport=httpx.MockTransport(answering(status, seen)))
    )  # type: ignore[arg-type]


def test_a_loops_contact_is_deleted_by_email() -> None:
    seen: list[httpx.Request] = []

    loops(200, seen).delete("ada@example.test")

    [request] = seen
    assert str(request.url) == "https://app.loops.so/api/v1/contacts/delete"
    assert request.content == b'{"email":"ada@example.test"}'
    assert request.headers["authorization"] == "Bearer loops-key"


def test_a_missing_loops_contact_is_done_and_a_refusal_raises_without_the_address() -> None:
    loops(404, []).delete("ada@example.test")
    with pytest.raises(ContactsError) as raised:
        loops(500, []).delete("ada@example.test")

    assert "500" in str(raised.value) and "ada@example.test" not in str(raised.value)

"""At most five session types per mentor, raisable by support (decision 286).

Every offering not truly deleted counts — live, hidden and scheduled for
deletion — and the limit is support's override or the default. The count runs
under a per-mentor advisory lock, so two creates at the edge cannot both land; a replayed
`Idempotency-Key` still answers its first `201`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type
from tests.integration.test_api_admin import make_user
from tests.integration.test_api_me_session_type_writes import URL, as_mentee, as_mentor, body

from app.domain.session_type_limit import DEFAULT_SESSION_TYPE_LIMIT, SESSION_TYPE_LIMIT_CEILING
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PROBLEM = "/problems/session-type-limit-reached"


def limit_url(user: UUID) -> str:
    return f"/api/v1/admin/mentors/{user}/session-type-limit"


async def held(engine: AsyncEngine, mentor: UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM session_types "
                        "WHERE mentor_user_id = :u AND deleted_at IS NULL"
                    ),
                    {"u": mentor},
                )
            ).scalar_one()
        )


async def give(engine: AsyncEngine, mentor: UUID, n: int, **kwargs: Any) -> None:
    for _ in range(n):
        await add_session_type(engine, mentor, name=f"Type {uuid4()}", **kwargs)


async def schedule_deletion(engine: AsyncEngine, mentor: UUID) -> None:
    """A hidden offering whose deletion waits on a booked session (#218)."""
    session_type = await add_session_type(engine, mentor, name=f"Leaving {uuid4()}", active=False)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET deletion_scheduled_at = now() WHERE id = :t"),
            {"t": session_type},
        )


async def set_override(engine: AsyncEngine, mentor: UUID, limit: int | None) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET session_type_limit = :n WHERE user_id = :u"),
            {"n": limit, "u": mentor},
        )


async def super_admin(engine: AsyncEngine, role: str = "super_admin", **kwargs: Any) -> str:
    auth = uuid4()
    await make_user(engine, auth, f"admin-{uuid4()}@example.com", role=role, **kwargs)
    return api_token(auth)


# --------------------------------------------------------------------------
# The create guard
# --------------------------------------------------------------------------


async def test_the_create_past_the_default_limit_is_a_typed_409(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "limit-full")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT - 1)

    fifth = await api_client.post(URL, json=body(name="Fifth"), headers=bearer(api_token(auth)))
    sixth = await api_client.post(URL, json=body(name="Sixth"), headers=bearer(api_token(auth)))

    assert fifth.status_code == 201, fifth.text
    assert sixth.status_code == 409, sixth.text
    assert sixth.json()["type"] == PROBLEM
    assert sixth.json()["limit"] == DEFAULT_SESSION_TYPE_LIMIT
    assert await held(db_engine, mentor) == DEFAULT_SESSION_TYPE_LIMIT


async def test_hidden_and_scheduled_offerings_hold_a_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "limit-hidden")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT - 2)
    await give(db_engine, mentor, 1, active=False)
    await schedule_deletion(db_engine, mentor)

    response = await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))

    assert response.status_code == 409, response.text
    assert response.json()["type"] == PROBLEM


async def test_a_scheduled_offering_alone_holds_its_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Apart from the hidden one, so a guard that counts `is_active` false but
    skips a scheduled row is caught."""
    mentor, auth = await as_mentor(db_engine, "limit-scheduled")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT - 1)
    await schedule_deletion(db_engine, mentor)

    response = await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))

    assert response.status_code == 409, response.text


async def test_a_deleted_offering_frees_its_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "limit-deleted")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT - 1)
    await give(db_engine, mentor, 3, deleted=True)

    response = await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))

    assert response.status_code == 201, response.text


async def test_a_raised_limit_admits_more(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "limit-raised")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT)
    await set_override(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT + 1)

    admitted = await api_client.post(URL, json=body(name="A"), headers=bearer(api_token(auth)))
    refused = await api_client.post(URL, json=body(name="B"), headers=bearer(api_token(auth)))

    assert admitted.status_code == 201, admitted.text
    assert refused.status_code == 409, refused.text
    assert refused.json()["limit"] == DEFAULT_SESSION_TYPE_LIMIT + 1


async def test_a_limit_below_what_is_held_refuses_and_deletes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "limit-lowered")
    await give(db_engine, mentor, 3)
    await set_override(db_engine, mentor, 1)

    response = await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))

    assert response.status_code == 409, response.text
    assert response.json()["limit"] == 1
    assert await held(db_engine, mentor) == 3


async def test_two_creates_at_the_edge_land_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The guard's lock serialises them; without it both count four and both land."""
    mentor, auth = await as_mentor(db_engine, "limit-race")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT - 1)
    headers = bearer(api_token(auth))

    answers = await asyncio.gather(
        *(api_client.post(URL, json=body(name=f"Race {n}"), headers=headers) for n in range(2))
    )

    assert sorted(a.status_code for a in answers) == [201, 409]
    assert await held(db_engine, mentor) == DEFAULT_SESSION_TYPE_LIMIT


async def test_two_keyed_creates_at_the_edge_land_one_and_never_deadlock(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Each claims its own `Idempotency-Key` first, whose row references the
    user; a row lock on that user taken after it would deadlock the pair."""
    mentor, auth = await as_mentor(db_engine, "limit-race-keyed")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT - 1)
    token = bearer(api_token(auth))

    answers = await asyncio.gather(
        *(
            api_client.post(
                URL, json=body(name=f"Keyed {n}"), headers=token | {"Idempotency-Key": f"k-{n}"}
            )
            for n in range(2)
        )
    )

    assert sorted(a.status_code for a in answers) == [201, 409], [a.text for a in answers]
    assert await held(db_engine, mentor) == DEFAULT_SESSION_TYPE_LIMIT


async def test_the_create_lock_does_not_wait_on_a_key_share_of_the_user(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The deadlock's precondition, made deterministic: another transaction's
    claimed key holds `FOR KEY SHARE` on the user row, as each keyed create's own
    claim does. A create whose lock conflicts with it would wait here, and two
    keyed creates would each wait on the other."""
    mentor, auth = await as_mentor(db_engine, "limit-key-share")
    async with db_engine.connect() as other:
        transaction = await other.begin()
        await other.execute(
            text(
                "INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash, locked_at) "
                "VALUES ('held', :u, 'elsewhere', 'h', now())"
            ),
            {"u": mentor},
        )
        try:
            response = await asyncio.wait_for(
                api_client.post(URL, json=body(), headers=bearer(api_token(auth))), timeout=5
            )
        finally:
            await transaction.rollback()

    assert response.status_code == 201, response.text


async def test_a_replay_of_the_create_that_filled_the_list_is_its_201(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Decision 196: the stored answer comes back, not the limit it now meets."""
    mentor, auth = await as_mentor(db_engine, "limit-replay")
    await give(db_engine, mentor, DEFAULT_SESSION_TYPE_LIMIT - 1)
    headers = bearer(api_token(auth)) | {"Idempotency-Key": "fill-1"}

    first = await api_client.post(URL, json=body(), headers=headers)
    again = await api_client.post(URL, json=body(), headers=headers)

    assert first.status_code == 201, first.text
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"


# --------------------------------------------------------------------------
# Support's override
# --------------------------------------------------------------------------


async def test_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    response = await api_client.put(limit_url(uuid4()), json={"limit": 7})

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("role", "revoked"),
    [(None, False), ("super_admin", True), ("mentor_approval", False), ("limited_access", False)],
    ids=["no-grant", "revoked", "mentor-approval", "limited-access"],
)
async def test_only_a_live_super_admin_may_set_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, role: str | None, revoked: bool
) -> None:
    mentor, _ = await as_mentor(db_engine, f"limit-who-{role}-{revoked}".lower())
    token = await super_admin(db_engine, role, revoked=revoked) if role else None
    if token is None:
        auth = uuid4()
        await make_user(db_engine, auth, f"nobody-{uuid4()}@example.com")
        token = api_token(auth)

    response = await api_client.put(limit_url(mentor), json={"limit": 7}, headers=bearer(token))

    assert response.status_code == 404, response.text
    async with db_engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT session_type_limit FROM mentor_profiles WHERE user_id = :u"),
                {"u": mentor},
            )
        ).scalar_one()
    assert stored is None


async def test_a_super_admin_raises_and_resets_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "limit-admin")
    headers = bearer(await super_admin(db_engine))

    raised = await api_client.put(limit_url(mentor), json={"limit": 7}, headers=headers)
    me_raised = await api_client.get("/api/v1/me", headers=bearer(api_token(auth)))
    reset = await api_client.put(limit_url(mentor), json={"limit": None}, headers=headers)
    me_reset = await api_client.get("/api/v1/me", headers=bearer(api_token(auth)))

    assert raised.status_code == 200, raised.text
    assert raised.json() == {"limit": 7, "is_default": False}
    assert me_raised.json()["session_type_limit"] == 7
    assert reset.json() == {"limit": DEFAULT_SESSION_TYPE_LIMIT, "is_default": True}
    assert me_reset.json()["session_type_limit"] == DEFAULT_SESSION_TYPE_LIMIT


async def test_a_limit_at_the_ceiling_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _ = await as_mentor(db_engine, "limit-ceiling")
    headers = bearer(await super_admin(db_engine))

    response = await api_client.put(
        limit_url(mentor), json={"limit": SESSION_TYPE_LIMIT_CEILING}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["limit"] == SESSION_TYPE_LIMIT_CEILING


@pytest.mark.parametrize("value", [0, SESSION_TYPE_LIMIT_CEILING + 1, "five", "7", True, 7.0])
async def test_a_limit_out_of_range_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: object
) -> None:
    mentor, _ = await as_mentor(db_engine, f"limit-range-{value}".lower())
    headers = bearer(await super_admin(db_engine))

    response = await api_client.put(limit_url(mentor), json={"limit": value}, headers=headers)

    assert response.status_code == 422, response.text


async def test_the_limit_must_be_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Null resets, so an omitted field must not reset by accident."""
    mentor, _ = await as_mentor(db_engine, "limit-missing")
    headers = bearer(await super_admin(db_engine))

    response = await api_client.put(limit_url(mentor), json={}, headers=headers)

    assert response.status_code == 422, response.text


async def test_a_user_who_is_not_a_mentor_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = bearer(await super_admin(db_engine))
    mentee_auth = await as_mentee(db_engine, "limit-mentee")
    me = await api_client.get("/api/v1/me", headers=bearer(api_token(mentee_auth)))

    response = await api_client.put(
        limit_url(UUID(me.json()["id"])), json={"limit": 7}, headers=headers
    )

    assert response.status_code == 404, response.text
    assert me.json()["session_type_limit"] is None


async def test_a_mentor_reads_the_default_on_me(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "limit-me")

    response = await api_client.get("/api/v1/me", headers=bearer(api_token(auth)))

    assert response.json()["session_type_limit"] == DEFAULT_SESSION_TYPE_LIMIT

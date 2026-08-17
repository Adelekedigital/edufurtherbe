"""The status log, the trigger that projects it, and who may write to it.

**The first test in this file is the one that matters.** It inserts an event
directly, with no application code involved, because that is the only thing that
distinguishes a working trigger from a caller that happens to write both the
event and the column. The first version of `apply_mentor_status` did not work at
all — PostgreSQL refuses a direct cast between two enum types — and every test
that went through the store would have passed.

`alembic check` cannot see triggers. This file is the only thing that does.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ADMIN = "/api/v1/admin"


async def make_user(
    engine: AsyncEngine, auth_id: UUID, email: str, *, role: str | None = None
) -> UUID:
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, primary_role, timezone) "
                    "VALUES (:e, :a, 'mentee', 'UTC') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()
        if role:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :r)"),
                {"u": user_id, "r": role},
            )
    return user_id


async def add_mentor(engine: AsyncEngine, user_id: UUID, *, approved: bool = False) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'I help')"),
            {"u": user_id},
        )
        if approved:
            # Through the log, because that is the only write path.
            await conn.execute(
                text(
                    "INSERT INTO mentor_status_events (mentor_user_id, status_type) "
                    "VALUES (:u, 'approved'), (:u, 'listed')"
                ),
                {"u": user_id},
            )


async def status_of(engine: AsyncEngine, user_id: UUID) -> tuple[str, str]:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT approval_status, listing_status FROM mentor_profiles WHERE user_id = :u"),
            {"u": user_id},
        )
        approval, listing = row.one()
    return str(approval), str(listing)


async def insert_event(
    engine: AsyncEngine, user_id: UUID, status_type: str, reason: str | None = None
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_status_events (mentor_user_id, status_type, reason) "
                "VALUES (:u, CAST(:s AS mentor_status_type), :r)"
            ),
            {"u": user_id, "s": status_type, "r": reason},
        )


# --------------------------------------------------------------------------
# The trigger
# --------------------------------------------------------------------------


async def test_an_event_inserted_directly_moves_the_column(db_engine: AsyncEngine) -> None:
    """**No application code in this test, deliberately.**

    Going through the store would pass whether the trigger worked or not,
    because the store would be writing the column too. Inserting by hand is what
    proves the projection exists — and the first version of it did not, because
    PostgreSQL refuses `mentor_status_type::listing_status` without `::text::`
    in between.
    """
    user_id = await make_user(db_engine, uuid4(), "mentor@example.com")
    await add_mentor(db_engine, user_id)

    assert await status_of(db_engine, user_id) == ("pending", "unlisted")

    await insert_event(db_engine, user_id, "approved")
    assert await status_of(db_engine, user_id) == ("approved", "unlisted")

    await insert_event(db_engine, user_id, "listed")
    assert await status_of(db_engine, user_id) == ("approved", "listed")


async def test_a_listing_event_leaves_approval_alone(db_engine: AsyncEngine) -> None:
    """The dimensions are separate, which is what lets one row state one fact
    without copying the other forward."""
    user_id = await make_user(db_engine, uuid4(), "mentor@example.com")
    await add_mentor(db_engine, user_id, approved=True)

    await insert_event(db_engine, user_id, "unlisted", "mentor_paused")

    assert await status_of(db_engine, user_id) == ("approved", "unlisted")


async def test_an_approval_event_leaves_listing_alone(db_engine: AsyncEngine) -> None:
    user_id = await make_user(db_engine, uuid4(), "mentor@example.com")
    await add_mentor(db_engine, user_id, approved=True)

    await insert_event(db_engine, user_id, "declined")

    assert await status_of(db_engine, user_id) == ("declined", "listed")


async def test_the_log_keeps_every_transition(db_engine: AsyncEngine) -> None:
    """The reason the table exists: a column can only describe the newest
    decision, and this mentor has four."""
    user_id = await make_user(db_engine, uuid4(), "mentor@example.com")
    await add_mentor(db_engine, user_id)
    for status in ("declined", "approved", "listed", "unlisted"):
        await insert_event(db_engine, user_id, status)

    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT status_type FROM mentor_status_events "
                "WHERE mentor_user_id = :u ORDER BY created_at, id"
            ),
            {"u": user_id},
        )

    assert [str(row[0]) for row in rows] == ["declined", "approved", "listed", "unlisted"]


# --------------------------------------------------------------------------
# An admin moving the listing without touching approval
# --------------------------------------------------------------------------


async def test_an_admin_can_unlist_an_approved_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The transition that made this log necessary.** Before it, listing only
    moved as a side effect of a decision, so "who unlisted this" always followed
    from the approval. It no longer does."""
    auth_id = uuid4()
    admin = await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    mentor = await make_user(db_engine, uuid4(), "mentor@example.com")
    await add_mentor(db_engine, mentor, approved=True)

    response = await api_client.post(
        f"{ADMIN}/mentors/{mentor}/listing",
        params={"listed": "false"},
        json={"reason": "admin_review"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 200
    assert await status_of(db_engine, mentor) == ("approved", "unlisted")
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT reason, created_by FROM mentor_status_events "
                "WHERE mentor_user_id = :u AND status_type = 'unlisted' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"u": mentor},
        )
        reason, created_by = row.one()
    assert (reason, created_by) == ("admin_review", admin)


async def test_history_reads_newest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    mentor = await make_user(db_engine, uuid4(), "mentor@example.com")
    await add_mentor(db_engine, mentor)
    for status in ("approved", "listed", "unlisted"):
        await insert_event(db_engine, mentor, status)

    body = (
        await api_client.get(
            f"{ADMIN}/mentors/{mentor}/history", headers=bearer(api_token(auth_id))
        )
    ).json()

    assert next(row["status_type"] for row in body["data"]) == "unlisted"
    # Null rather than invented: nobody acted on the rows a migration writes.
    assert body["data"][0]["created_by"] is None


# --------------------------------------------------------------------------
# A mentor pausing themselves — and not undoing an admin
# --------------------------------------------------------------------------


async def test_a_mentor_can_pause_and_resume_themselves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    mentor = await make_user(db_engine, auth_id, "mentor@example.com")
    await add_mentor(db_engine, mentor, approved=True)
    headers = bearer(api_token(auth_id))

    paused = await api_client.post(f"/api/v1/users/{mentor}/mentor-profile/pause", headers=headers)
    after_pause = await status_of(db_engine, mentor)
    resumed = await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume", headers=headers
    )

    assert (paused.status_code, resumed.status_code) == (200, 200)
    assert after_pause == ("approved", "unlisted")
    assert await status_of(db_engine, mentor) == ("approved", "listed")


async def test_a_mentor_cannot_resume_an_admin_unlisting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Otherwise a suspension is a button the suspended person can press.**

    The newest unlisting's reason decides it: their own pause says
    `mentor_paused`, an admin's says something else.
    """
    admin_auth, mentor_auth = uuid4(), uuid4()
    await make_user(db_engine, admin_auth, "admin@example.com", role="super_admin")
    mentor = await make_user(db_engine, mentor_auth, "mentor@example.com")
    await add_mentor(db_engine, mentor, approved=True)

    await api_client.post(
        f"{ADMIN}/mentors/{mentor}/listing",
        params={"listed": "false"},
        json={"reason": "admin_review"},
        headers=bearer(api_token(admin_auth)),
    )
    response = await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume", headers=bearer(api_token(mentor_auth))
    )

    assert response.status_code == 404
    assert await status_of(db_engine, mentor) == ("approved", "unlisted")


async def test_a_mentor_who_was_never_approved_cannot_list_themselves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """They have nothing to return to, and relisting would put an unapproved
    profile in the directory."""
    auth_id = uuid4()
    mentor = await make_user(db_engine, auth_id, "mentor@example.com")
    await add_mentor(db_engine, mentor)

    response = await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume", headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 404
    assert await status_of(db_engine, mentor) == ("pending", "unlisted")


async def test_a_pending_mentor_cannot_resume_their_own_pause(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Isolates the **approval** half of the resume rule.

    A never-approved mentor has no unlisting at all, so the reason check alone
    refuses them and a mutation removing the approval check survives. This one
    pauses first — so the reason *is* `mentor_paused` and only the approval
    check stands between them and a listed, unapproved profile.
    """
    auth_id = uuid4()
    mentor = await make_user(db_engine, auth_id, "mentor@example.com")
    await add_mentor(db_engine, mentor)
    headers = bearer(api_token(auth_id))
    await api_client.post(f"/api/v1/users/{mentor}/mentor-profile/pause", headers=headers)

    response = await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume", headers=headers
    )

    assert response.status_code == 404
    assert await status_of(db_engine, mentor) == ("pending", "unlisted")


async def test_only_the_newest_unlisting_decides(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two unlistings, and the **newest** is the one that counts.

    Paused by the mentor, then unlisted by an admin. With one unlisting the
    oldest and newest are the same row, so a mutation reading the wrong end
    survives — and reading the oldest here would let the mentor undo the
    admin's suspension because their own pause came first.
    """
    admin_auth, mentor_auth = uuid4(), uuid4()
    await make_user(db_engine, admin_auth, "admin@example.com", role="super_admin")
    mentor = await make_user(db_engine, mentor_auth, "mentor@example.com")
    await add_mentor(db_engine, mentor, approved=True)

    await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/pause", headers=bearer(api_token(mentor_auth))
    )
    await api_client.post(
        f"{ADMIN}/mentors/{mentor}/listing",
        params={"listed": "false"},
        json={"reason": "admin_review"},
        headers=bearer(api_token(admin_auth)),
    )

    response = await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume", headers=bearer(api_token(mentor_auth))
    )

    assert response.status_code == 404, "the mentor undid an admin unlisting that came after theirs"
    assert await status_of(db_engine, mentor) == ("approved", "unlisted")


async def test_a_user_with_no_mentor_profile_writes_no_event(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Their **own** URL, so the ownership dependency passes and the store's
    existence check is the only thing left — the shape that has hidden three
    scope defects in this project already.

    Without it the insert reaches a foreign key pointing at `mentor_profiles`
    and the caller gets a 500 where a 404 belongs.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "notamentor@example.com")

    response = await api_client.post(
        f"/api/v1/users/{user_id}/mentor-profile/pause", headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 404
    async with db_engine.connect() as conn:
        rows = await conn.execute(text("SELECT count(*) FROM mentor_status_events"))
    assert rows.scalar_one() == 0


async def test_pausing_records_the_mentor_as_the_actor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    mentor = await make_user(db_engine, auth_id, "mentor@example.com")
    await add_mentor(db_engine, mentor, approved=True)

    await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/pause", headers=bearer(api_token(auth_id))
    )

    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT reason, created_by FROM mentor_status_events "
                "WHERE mentor_user_id = :u AND status_type = 'unlisted' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"u": mentor},
        )
        reason, created_by = row.one()

    assert (reason, created_by) == ("mentor_paused", mentor)


# --------------------------------------------------------------------------
# Who may reach the new endpoints
# --------------------------------------------------------------------------

NEW_ADMIN: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "/mentors/{id}/listing", {"reason": None}),
    ("get", "/mentors/{id}/history", None),
]


@pytest.mark.parametrize(("method", "path", "body"), NEW_ADMIN, ids=[p for _, p, _ in NEW_ADMIN])
async def test_a_user_with_no_grant_is_refused(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "nobody@example.com")

    response = await api_client.request(
        method.upper(),
        ADMIN + path.replace("{id}", str(uuid4())),
        json=body,
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), NEW_ADMIN, ids=[p for _, p, _ in NEW_ADMIN])
async def test_no_token_is_refused(
    api_client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = await api_client.request(
        method.upper(), ADMIN + path.replace("{id}", str(uuid4())), json=body
    )

    assert response.status_code == 401


async def test_a_mentor_cannot_pause_somebody_else(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    caller_auth = uuid4()
    await make_user(db_engine, caller_auth, "caller@example.com")
    other = await make_user(db_engine, uuid4(), "mentor@example.com")
    await add_mentor(db_engine, other, approved=True)

    response = await api_client.post(
        f"/api/v1/users/{other}/mentor-profile/pause", headers=bearer(api_token(caller_auth))
    )

    assert response.status_code == 404
    assert await status_of(db_engine, other) == ("approved", "listed")

"""Notes, bulk approve, and the pause reason and email (decision 298).

Reads take every live grant; writes take `mentor_approval` or super admin, so
`limited_access` is refused (404) on each write as a caller with no grant is.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    admin_token,
    applicant,
    approved,
    make_public_mentor,
    status_event,
)
from tests.integration.test_api_admin import make_user, queued_for

from app.domain.admin_notes import NOTE_BODY_MAX_LENGTH
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ADMIN = "/api/v1/admin"


async def outbox(engine: AsyncEngine, mentor: UUID, event_type: str) -> list[Any]:
    """What is queued to ``mentor`` of one kind, each as its variables."""
    return [extras for kind, extras in await queued_for(engine, mentor) if kind == event_type]


async def state(engine: AsyncEngine, mentor: UUID) -> tuple[str, str]:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT approval_status, listing_status FROM mentor_profiles WHERE user_id = :m"
                ),
                {"m": mentor},
            )
        ).one()
        return str(row[0]), str(row[1])


async def deciding_admin(engine: AsyncEngine) -> tuple[str, UUID]:
    auth_id = uuid4()
    admin = await make_user(engine, auth_id, f"d-{auth_id}@example.test", role="mentor_approval")
    return api_token(auth_id), admin


# --------------------------------------------------------------------------
# Who may reach it
# --------------------------------------------------------------------------

WRITES: list[tuple[str, dict[str, Any]]] = [
    ("/mentors/{id}/notes", {"body": "Looks strong"}),
    ("/mentors/approvals", {"user_ids": ["{id}"]}),
]


def _filled(body: dict[str, Any], mentor: UUID) -> dict[str, Any]:
    return {key: ([str(mentor)] if value == ["{id}"] else value) for key, value in body.items()}


@pytest.mark.parametrize(("path", "body"), WRITES, ids=[p for p, _ in WRITES])
@pytest.mark.parametrize("who", ["none", "no-grant", "revoked", "limited"])
async def test_a_write_takes_a_deciding_grant(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    path: str,
    body: dict[str, Any],
    who: str,
) -> None:
    mentor = await applicant(db_engine, f"w-{who}")
    headers: dict[str, str] = {}
    if who == "limited":
        headers = bearer(await admin_token(db_engine, "limited_access"))
    elif who != "none":
        auth_id = uuid4()
        await make_user(
            db_engine,
            auth_id,
            f"{who}@example.test",
            role="super_admin" if who == "revoked" else None,
            revoked=who == "revoked",
        )
        headers = bearer(api_token(auth_id))

    response = await api_client.post(
        ADMIN + path.replace("{id}", str(mentor)), json=_filled(body, mentor), headers=headers
    )

    assert response.status_code == (401 if who == "none" else 404)
    assert await state(db_engine, mentor) == ("pending", "unlisted")


@pytest.mark.parametrize("who", ["none", "no-grant", "revoked"])
async def test_reading_notes_takes_a_live_grant(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    mentor = await applicant(db_engine, f"r-{who}")
    headers: dict[str, str] = {}
    if who != "none":
        auth_id = uuid4()
        await make_user(
            db_engine,
            auth_id,
            f"{who}@example.test",
            role="super_admin" if who == "revoked" else None,
            revoked=who == "revoked",
        )
        headers = bearer(api_token(auth_id))

    response = await api_client.get(f"{ADMIN}/mentors/{mentor}/notes", headers=headers)

    assert response.status_code == (401 if who == "none" else 404)


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------


async def test_notes_are_written_trimmed_and_read_oldest_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, admin = await deciding_admin(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET first_name = 'Kemi', last_name = 'Ade' WHERE id = :a"),
            {"a": admin},
        )
    mentor = await applicant(db_engine, "noted")

    first = await api_client.post(
        f"{ADMIN}/mentors/{mentor}/notes",
        json={"body": "  Checked LinkedIn  "},
        headers=bearer(token),
    )
    await api_client.post(
        f"{ADMIN}/mentors/{mentor}/notes", json={"body": "Second look"}, headers=bearer(token)
    )
    listed = await api_client.get(
        f"{ADMIN}/mentors/{mentor}/notes",
        headers=bearer(await admin_token(db_engine, "limited_access")),
    )

    assert first.status_code == 201, first.text
    assert first.headers["location"] == f"{ADMIN}/mentors/{mentor}/notes"
    created = first.json()
    assert created["body"] == "Checked LinkedIn"
    assert created["author"] == {
        "user_id": str(admin),
        "first_name": "Kemi",
        "last_name": "Ade",
        "avatar_url": None,
        "cover_color": None,
    }
    assert listed.status_code == 200
    assert [note["body"] for note in listed.json()["data"]] == ["Checked LinkedIn", "Second look"]


@pytest.mark.parametrize(
    "body", ["", "   ", "x" * (NOTE_BODY_MAX_LENGTH + 1)], ids=["empty", "blank", "long"]
)
async def test_a_note_must_say_something_within_the_bound(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: str
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "bounded")

    response = await api_client.post(
        f"{ADMIN}/mentors/{mentor}/notes", json={"body": body}, headers=bearer(token)
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/body"


async def test_a_note_at_the_bound_is_kept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "at-bound")

    response = await api_client.post(
        f"{ADMIN}/mentors/{mentor}/notes",
        json={"body": "x" * NOTE_BODY_MAX_LENGTH},
        headers=bearer(token),
    )

    assert response.status_code == 201


@pytest.mark.parametrize("who", ["not-a-mentor", "deleted", "nobody"])
async def test_notes_need_a_mentor_who_exists(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    token, _ = await deciding_admin(db_engine)
    if who == "not-a-mentor":
        target = await make_user(db_engine, uuid4(), "plain@example.test")
    elif who == "deleted":
        target = await applicant(db_engine, "gone")
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET deleted_at = now() WHERE id = :m"), {"m": target}
            )
    else:
        target = uuid4()

    write = await api_client.post(
        f"{ADMIN}/mentors/{target}/notes", json={"body": "Hm"}, headers=bearer(token)
    )
    read = await api_client.get(f"{ADMIN}/mentors/{target}/notes", headers=bearer(token))

    assert (write.status_code, read.status_code) == (404, 404)


async def test_a_departed_author_keeps_the_note_and_loses_the_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, admin = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "kept-note")
    await api_client.post(
        f"{ADMIN}/mentors/{mentor}/notes", json={"body": "Mine"}, headers=bearer(token)
    )
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :a"), {"a": admin})

    read = await api_client.get(
        f"{ADMIN}/mentors/{mentor}/notes",
        headers=bearer(await admin_token(db_engine, "limited_access")),
    )

    (note,) = read.json()["data"]
    assert note["body"] == "Mine"
    assert note["author"]["user_id"] == str(admin)
    assert note["author"]["first_name"] is None


# --------------------------------------------------------------------------
# Bulk approve
# --------------------------------------------------------------------------


async def test_bulk_approve_approves_applications_and_reports_the_rest(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    first = await applicant(db_engine, "bulk-1")
    second = await applicant(db_engine, "bulk-2")
    draft = await make_public_mentor(db_engine, "bulk-draft", approved=False, listed=False)
    already = await approved(db_engine, "bulk-done")
    nobody = uuid4()

    response = await api_client.post(
        f"{ADMIN}/mentors/approvals",
        json={
            "user_ids": [str(first), str(draft), str(second), str(first), str(already), str(nobody)]
        },
        headers=bearer(token),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "approved": [str(first), str(second)],
        "failed": [str(draft), str(already), str(nobody)],
    }
    assert await state(db_engine, first) == ("approved", "listed")
    assert await state(db_engine, second) == ("approved", "listed")
    assert await state(db_engine, draft) == ("pending", "unlisted")
    assert len(await outbox(db_engine, first, "mentor_approved")) == 1
    assert await outbox(db_engine, already, "mentor_approved") == []


async def test_a_second_bulk_approve_approves_nobody_twice(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "twice")
    body = {"user_ids": [str(mentor)]}

    await api_client.post(f"{ADMIN}/mentors/approvals", json=body, headers=bearer(token))
    again = await api_client.post(f"{ADMIN}/mentors/approvals", json=body, headers=bearer(token))

    assert again.json() == {"approved": [], "failed": [str(mentor)]}
    assert len(await outbox(db_engine, mentor, "mentor_approved")) == 1


async def test_one_mentor_failing_undoes_nobody_else(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A database error on one mentor is rolled back and reported for that
    mentor alone: the ones before and after it are approved and emailed."""
    token, _ = await deciding_admin(db_engine)
    before = await applicant(db_engine, "before")
    broken = await applicant(db_engine, "broken")
    after = await applicant(db_engine, "after")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION refuse_one() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF NEW.mentor_user_id = '{broken}' THEN RAISE EXCEPTION 'boom'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER refuse_one BEFORE INSERT ON mentor_status_events "
                "FOR EACH ROW EXECUTE FUNCTION refuse_one()"
            )
        )

    response = await api_client.post(
        f"{ADMIN}/mentors/approvals",
        json={"user_ids": [str(before), str(broken), str(after)]},
        headers=bearer(token),
    )

    assert response.json() == {"approved": [str(before), str(after)], "failed": [str(broken)]}
    assert await state(db_engine, broken) == ("pending", "unlisted")
    assert await outbox(db_engine, broken, "mentor_approved") == []
    assert len(await outbox(db_engine, after, "mentor_approved")) == 1
    assert await state(db_engine, after) == ("approved", "listed")


async def test_a_bulk_decline_is_refused_rather_than_approving(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """There is no bulk decline: a client sending one gets a `422`, never a
    silent approval of everyone it meant to turn down."""
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "decline-attempt")

    response = await api_client.post(
        f"{ADMIN}/mentors/approvals",
        json={"user_ids": [str(mentor)], "approve": False},
        headers=bearer(token),
    )

    assert response.status_code == 422
    assert await state(db_engine, mentor) == ("pending", "unlisted")


@pytest.mark.parametrize("ids", [[], [str(uuid4()) for _ in range(51)]], ids=["none", "51"])
async def test_bulk_approve_takes_one_to_fifty(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, ids: list[str]
) -> None:
    token, _ = await deciding_admin(db_engine)

    response = await api_client.post(
        f"{ADMIN}/mentors/approvals", json={"user_ids": ids}, headers=bearer(token)
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Pause and resume
# --------------------------------------------------------------------------


async def listing(
    client: httpx.AsyncClient, token: str, mentor: UUID, *, listed: bool, reason: str | None
) -> httpx.Response:
    return await client.post(
        f"{ADMIN}/mentors/{mentor}/listing",
        params={"listed": str(listed).lower()},
        json={"reason": reason},
        headers=bearer(token),
    )


@pytest.mark.parametrize("reason", [None, "", "   "], ids=["null", "empty", "blank"])
async def test_pausing_needs_a_reason(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, reason: str | None
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await approved(db_engine, "no-reason")

    response = await listing(api_client, token, mentor, listed=False, reason=reason)

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/reason"
    assert await state(db_engine, mentor) == ("approved", "listed")
    assert await outbox(db_engine, mentor, "mentor_paused_by_admin") == []


async def test_pausing_hides_the_mentor_and_tells_them_without_the_reason(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await approved(db_engine, "paused")

    response = await listing(api_client, token, mentor, listed=False, reason="Two complaints")
    again = await listing(api_client, token, mentor, listed=False, reason="Still reviewing")

    assert (response.status_code, again.status_code) == (200, 200)
    assert await state(db_engine, mentor) == ("approved", "unlisted")
    # Once, to the mentor, and with no variables: never the admin's reason.
    assert await outbox(db_engine, mentor, "mentor_paused_by_admin") == [{}]


async def test_pausing_over_a_mentors_own_pause_tells_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await approved(db_engine, "own-then-admin")
    await status_event(db_engine, mentor, "unlisted", by=mentor, reason="mentor_paused")

    response = await listing(api_client, token, mentor, listed=False, reason="Hold")

    assert response.status_code == 200
    assert len(await outbox(db_engine, mentor, "mentor_paused_by_admin")) == 1


@pytest.mark.parametrize("who", ["applicant", "self-paused-applicant", "declined"])
async def test_unlisting_somebody_never_approved_sends_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    """An applicant may pause themselves; an admin unlisting them is still no
    news, because they were never on Explore."""
    token, admin = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, f"never-{who}")
    if who == "self-paused-applicant":
        await status_event(db_engine, mentor, "unlisted", by=mentor, reason="mentor_paused")
    if who == "declined":
        await status_event(db_engine, mentor, "declined", by=admin, reason="No")

    response = await listing(api_client, token, mentor, listed=False, reason="Spam")

    assert response.status_code == 200
    assert await outbox(db_engine, mentor, "mentor_paused_by_admin") == []


async def test_resuming_relists_and_sends_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, admin = await deciding_admin(db_engine)
    mentor = await approved(db_engine, "resumed")
    await status_event(db_engine, mentor, "unlisted", by=admin, reason="Hold")

    response = await listing(api_client, token, mentor, listed=True, reason=None)

    assert response.status_code == 200
    assert await state(db_engine, mentor) == ("approved", "listed")
    assert await outbox(db_engine, mentor, "mentor_paused_by_admin") == []


async def test_a_mentor_who_paused_themselves_is_theirs_to_resume(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await approved(db_engine, "self-paused")
    await status_event(db_engine, mentor, "unlisted", by=mentor, reason="mentor_paused")

    response = await listing(api_client, token, mentor, listed=True, reason=None)

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/mentor-paused-themselves"
    assert await state(db_engine, mentor) == ("approved", "unlisted")

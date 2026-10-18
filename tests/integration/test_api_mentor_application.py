"""Submitting a mentor application, and the owner reading where it stands
(Mentor Onboarding PRD, B2)."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.application_factories import make_applicant
from tests.integration.factories import make_public_mentor, token_for

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

SUBMIT = "/api/v1/me/mentor-application/submit"


async def submit(client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID) -> httpx.Response:
    return await client.post(SUBMIT, headers=bearer(await token_for(engine, mentor)))


async def alerts(engine: AsyncEngine, mentor: UUID) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'mentor_application_received' AND entity_id = :i"
                    ),
                    {"i": mentor},
                )
            ).scalar_one()
        )


async def an_admin(engine: AsyncEngine, tag: str) -> UUID:
    admin = await make_public_mentor(engine, f"admin-{tag}")
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'mentor_approval')"),
            {"u": admin},
        )
    return admin


async def run(engine: AsyncEngine, sql: str, mentor: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), {"u": mentor})


# --------------------------------------------------------------------------
# Submitting
# --------------------------------------------------------------------------


async def test_a_complete_application_is_submitted_and_the_admins_told_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await an_admin(db_engine, "once")
    mentor = await make_applicant(db_engine, "submit-once")

    first = await submit(api_client, db_engine, mentor)
    second = await submit(api_client, db_engine, mentor)

    assert first.status_code == 200, first.text
    assert first.json()["approval_status"] == "pending"
    assert first.json()["submitted_at"] is not None
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert await alerts(db_engine, mentor) == 1


# Each statement removes exactly one item from a complete application.
GAPS = {
    "offerings": "DELETE FROM mentor_service_offerings WHERE mentor_user_id = :u",
    "education": "UPDATE education_entries SET date_start = NULL WHERE user_id = :u",
    "primary_study_country": (
        "UPDATE mentor_profiles SET primary_study_country_id = NULL WHERE user_id = :u"
    ),
    "avatar": "UPDATE user_profiles SET avatar_url = NULL WHERE user_id = :u",
    "origin_country": "UPDATE user_profiles SET origin_country_id = NULL WHERE user_id = :u",
    "social_linkedin": "UPDATE user_profiles SET social_linkedin = NULL WHERE user_id = :u",
    "headline": "UPDATE mentor_profiles SET headline = '   ' WHERE user_id = :u",
    "availability": "UPDATE availability_rules SET is_active = false WHERE mentor_user_id = :u",
    "session_type": "UPDATE session_types SET is_active = false WHERE mentor_user_id = :u",
}


@pytest.mark.parametrize(("item", "gap"), GAPS.items(), ids=GAPS.keys())
async def test_each_missing_item_is_a_409_naming_it_and_nothing_is_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, item: str, gap: str
) -> None:
    await an_admin(db_engine, f"gap-{item}")
    mentor = await make_applicant(db_engine, f"gap-{item}")
    await run(db_engine, gap, mentor)

    response = await submit(api_client, db_engine, mentor)

    assert response.status_code == 409, response.text
    assert response.json()["type"] == "/problems/mentor-application-incomplete"
    assert response.json()["missing"] == [item]
    assert await alerts(db_engine, mentor) == 0


@pytest.mark.parametrize(
    ("item", "gap"),
    [
        ("offerings", "UPDATE service_offerings SET is_active = false"),
        ("education", "UPDATE education_entries SET deleted_at = now() WHERE user_id = :u"),
        ("education", "UPDATE education_entries SET degree_level_id = NULL WHERE user_id = :u"),
        ("education", "UPDATE education_entries SET study_course = NULL WHERE user_id = :u"),
        ("education", "UPDATE education_entries SET date_end = NULL WHERE user_id = :u"),
        ("session_type", "UPDATE session_types SET deleted_at = now() WHERE mentor_user_id = :u"),
        (
            "availability",
            "UPDATE availability_rules SET deleted_at = now() WHERE mentor_user_id = :u",
        ),
    ],
    ids=["retired-offering", "deleted", "no-degree", "no-field", "no-end", "deleted", "deleted"],
)
async def test_a_retired_or_deleted_item_counts_for_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, item: str, gap: str
) -> None:
    mentor = await make_applicant(db_engine, f"stale-{item}-{abs(hash(gap))}")
    await run(db_engine, gap, mentor)

    response = await submit(api_client, db_engine, mentor)

    assert response.status_code == 409, response.text
    assert item in response.json()["missing"]


async def test_an_empty_application_names_every_item(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_applicant(db_engine, "submit-empty", complete=False)
    await run(db_engine, "UPDATE mentor_profiles SET headline = NULL WHERE user_id = :u", mentor)

    response = await submit(api_client, db_engine, mentor)

    assert response.status_code == 409, response.text
    assert response.json()["missing"] == list(GAPS)


@pytest.mark.parametrize("decided", ["approved", "declined"])
async def test_a_decided_application_cannot_be_submitted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, decided: str
) -> None:
    mentor = await make_applicant(db_engine, f"submit-{decided}")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET approval_status = :s WHERE user_id = :u"),
            {"s": decided, "u": mentor},
        )

    response = await submit(api_client, db_engine, mentor)

    assert response.status_code == 409, response.text
    assert response.json()["type"] == "/problems/mentor-application-decided"
    assert await alerts(db_engine, mentor) == 0


async def test_someone_with_no_mentor_profile_gets_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_applicant(db_engine, "submit-gone")
    await run(db_engine, "UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u", mentor)

    response = await submit(api_client, db_engine, mentor)

    assert response.status_code == 404, response.text


async def test_submitting_needs_a_token(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.post(SUBMIT)).status_code == 401


async def test_an_applicant_who_is_an_admin_is_not_told_about_themselves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_applicant(db_engine, "submit-self-admin")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": mentor},
        )

    response = await submit(api_client, db_engine, mentor)

    assert response.status_code == 200, response.text
    assert await alerts(db_engine, mentor) == 0


# --------------------------------------------------------------------------
# Creating a profile is not applying
# --------------------------------------------------------------------------


async def test_creating_a_mentor_profile_tells_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await an_admin(db_engine, "create")
    applicant = await make_public_mentor(db_engine, "create-applicant")
    await run(db_engine, "DELETE FROM mentor_profiles WHERE user_id = :u", applicant)

    response = await api_client.post(
        f"/api/v1/users/{applicant}/mentor-profile",
        headers=bearer(await token_for(db_engine, applicant)),
        json={"headline": "I help with applications"},
    )

    assert response.status_code == 201, response.text
    assert await alerts(db_engine, applicant) == 0


# --------------------------------------------------------------------------
# Reading where it stands
# --------------------------------------------------------------------------


async def own_profile(client: httpx.AsyncClient, engine: AsyncEngine, user: UUID) -> dict:  # type: ignore[type-arg]
    response = await client.get("/api/v1/me", headers=bearer(await token_for(engine, user)))
    assert response.status_code == 200, response.text
    return response.json()["mentor_profile"]  # type: ignore[no-any-return]


async def decline(engine: AsyncEngine, mentor: UUID, reason: str | None, at: str) -> None:
    """A decline event at `at`; the trigger projects it onto `approval_status`."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_status_events "
                "(mentor_user_id, status_type, reason, created_at) "
                "VALUES (:u, 'declined', :r, :at)"
            ),
            {"u": mentor, "r": reason, "at": dt.datetime.fromisoformat(at)},
        )


async def test_the_owner_reads_when_they_submitted_and_nothing_else_yet(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_applicant(db_engine, "read-submitted")
    before = await own_profile(api_client, db_engine, mentor)
    submitted = (await submit(api_client, db_engine, mentor)).json()["submitted_at"]

    after = await own_profile(api_client, db_engine, mentor)

    assert before["submitted_at"] is None
    assert after["submitted_at"] == submitted
    assert after["approved_seen_at"] is None
    assert after["review_note"] is None
    assert after["reviewed_at"] is None


async def test_a_declined_owner_reads_the_latest_note_and_its_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_applicant(db_engine, "read-declined")
    await decline(db_engine, mentor, "Older note", "2026-09-01T10:00:00Z")
    await decline(db_engine, mentor, "Add your LinkedIn", "2026-10-01T10:00:00Z")

    profile = await own_profile(api_client, db_engine, mentor)

    assert profile["approval_status"] == "declined"
    assert profile["review_note"] == "Add your LinkedIn"
    assert profile["reviewed_at"].startswith("2026-10-01T10:00:00")


async def test_the_note_goes_once_the_mentor_is_approved(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_applicant(db_engine, "read-reapproved")
    await decline(db_engine, mentor, "Add your LinkedIn", "2026-09-01T10:00:00Z")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_status_events (mentor_user_id, status_type) "
                "VALUES (:u, 'approved')"
            ),
            {"u": mentor},
        )

    profile = await own_profile(api_client, db_engine, mentor)

    assert profile["approval_status"] == "approved"
    assert profile["review_note"] is None
    assert profile["reviewed_at"] is None


async def test_an_admin_reading_the_profile_gets_none_of_the_owner_fields(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The guardrail: a review note is shown to nobody but its applicant."""
    admin = await an_admin(db_engine, "reader")
    mentor = await make_applicant(db_engine, "read-by-admin")
    await submit(api_client, db_engine, mentor)
    await decline(db_engine, mentor, "Add your LinkedIn", "2026-10-01T10:00:00Z")

    owner_view = await api_client.get(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=bearer(await token_for(db_engine, mentor)),
    )
    admin_view = await api_client.get(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=bearer(await token_for(db_engine, admin)),
    )

    assert owner_view.status_code == 200, owner_view.text
    assert owner_view.json()["review_note"] == "Add your LinkedIn"
    assert owner_view.json()["submitted_at"] is not None
    assert admin_view.status_code == 200, admin_view.text
    for field in ("submitted_at", "approved_seen_at", "review_note", "reviewed_at"):
        assert admin_view.json()[field] is None

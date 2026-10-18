"""Approved-seen, the admin queue, the mentor onboarding steps and switching to
the mentee dashboard (Mentor Onboarding PRD, B3)."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.application_factories import an_admin, make_applicant, run_sql
from tests.integration.factories import make_public_mentor, token_for

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

SEEN = "/api/v1/me/mentor-application/approved-seen"
QUEUE = "/api/v1/admin/mentors/pending"


async def headers(engine: AsyncEngine, user: UUID) -> dict[str, str]:
    return bearer(await token_for(engine, user))


# --------------------------------------------------------------------------
# Approved-seen
# --------------------------------------------------------------------------


async def test_an_approved_mentor_marks_the_approval_seen_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "seen-once")
    auth = await headers(db_engine, mentor)

    first = await api_client.post(SEEN, headers=auth)
    second = await api_client.post(SEEN, headers=auth)
    me = await api_client.get("/api/v1/me", headers=auth)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert me.json()["mentor_profile"]["approved_seen_at"] == first.json()["approved_seen_at"]


@pytest.mark.parametrize("status", ["pending", "declined"])
async def test_only_an_approved_mentor_has_an_approval_to_see(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    mentor = await make_applicant(db_engine, f"seen-{status}")
    await run_sql(
        db_engine,
        "UPDATE mentor_profiles SET approval_status = :s WHERE user_id = :u",
        {"s": status, "u": mentor},
    )

    response = await api_client.post(SEEN, headers=await headers(db_engine, mentor))

    assert response.status_code == 409, response.text
    me = await api_client.get("/api/v1/me", headers=await headers(db_engine, mentor))
    assert me.json()["mentor_profile"]["approved_seen_at"] is None


async def test_a_decline_resets_seen_so_a_later_approval_shows_its_screen(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine, "reseen")
    mentor = await make_public_mentor(db_engine, "seen-again")
    auth = await headers(db_engine, mentor)
    await api_client.post(SEEN, headers=auth)
    decide = f"/api/v1/admin/mentors/{mentor}/decision"
    admin_auth = await headers(db_engine, admin)

    declined = await api_client.post(
        decide, params={"approve": "false"}, json={}, headers=admin_auth
    )
    approved = await api_client.post(decide, json={}, headers=admin_auth)
    me = (await api_client.get("/api/v1/me", headers=auth)).json()

    assert declined.status_code == 200, declined.text
    assert approved.status_code == 200, approved.text
    assert me["mentor_profile"]["approval_status"] == "approved"
    assert me["mentor_profile"]["approved_seen_at"] is None


async def test_approved_seen_without_a_mentor_profile_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "seen-gone")
    await run_sql(
        db_engine, "UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u", {"u": mentor}
    )

    response = await api_client.post(SEEN, headers=await headers(db_engine, mentor))

    assert response.status_code == 404, response.text


async def test_approved_seen_needs_a_token(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.post(SEEN)).status_code == 401


# --------------------------------------------------------------------------
# The admin queue
# --------------------------------------------------------------------------


async def test_the_queue_lists_submitted_applications_oldest_submission_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine, "queue")
    later = await make_applicant(db_engine, "queue-later")
    earlier = await make_applicant(db_engine, "queue-earlier")
    draft = await make_applicant(db_engine, "queue-draft")
    for mentor, at in ((later, "2026-10-02T00:00:00Z"), (earlier, "2026-10-01T00:00:00Z")):
        await run_sql(
            db_engine,
            "UPDATE mentor_profiles SET submitted_at = CAST(:at AS text)::timestamptz "
            "WHERE user_id = :u",
            {"at": at, "u": mentor},
        )

    body = (await api_client.get(QUEUE, headers=await headers(db_engine, admin))).json()

    assert [row["user_id"] for row in body["data"]] == [str(earlier), str(later)]
    assert body["data"][0]["submitted_at"].startswith("2026-10-01T00:00:00")
    assert str(draft) not in {row["user_id"] for row in body["data"]}


# --------------------------------------------------------------------------
# Onboarding steps and the dashboard switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "step", ["mentor_help", "mentor_education", "mentor_about", "mentor_hours"]
)
async def test_a_mentor_onboarding_step_is_saved_and_read_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, step: str
) -> None:
    mentor = await make_applicant(db_engine, f"step-{step}", complete=False)
    auth = await headers(db_engine, mentor)

    saved = await api_client.put("/api/v1/me/onboarding", headers=auth, json={"last_step": step})
    me = await api_client.get("/api/v1/me", headers=auth)

    assert saved.status_code == 200, saved.text
    assert saved.json()["last_step"] == step
    assert me.json()["onboarding"]["last_step"] == step


async def test_continue_as_a_mentee_switches_the_dashboard_and_keeps_the_application(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_applicant(db_engine, "role-mentee")
    auth = await headers(db_engine, mentor)

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/profile", headers=auth, json={"primary_role": "mentee"}
    )
    me = (await api_client.get("/api/v1/me", headers=auth)).json()

    assert response.status_code == 204, response.text
    assert me["primary_role"] == "mentee"
    assert me["mentor_profile"] is not None


@pytest.mark.parametrize("value", [None, "admin", ""])
async def test_primary_role_must_be_mentee_or_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: object
) -> None:
    mentor = await make_applicant(db_engine, f"role-bad-{str(value).lower()}")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/profile",
        headers=await headers(db_engine, mentor),
        json={"primary_role": value},
    )

    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["pointer"] == "/primary_role"

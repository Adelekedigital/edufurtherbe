"""The admin Mentors page: summary, tabs and the drawer (decision 297).

Each read is an admin read, so each gets the four authorization cases (no token,
no grant, a revoked grant, a live one). The rest pins who each tab holds and
that every figure is the rule it reuses.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_education,
    add_session,
    admin_token,
    applicant,
    approved,
    make_public_mentor,
    status_event,
    token_for,
)
from tests.integration.test_api_admin import make_user

from app.domain.admin_mentors import AdminMentorTab
from app.infra.db.admin_mentors import tab_statement
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ADMIN = "/api/v1/admin"

READS = ["/mentors/summary", "/mentors?tab=applications", "/mentors/{id}"]


async def rows(client: httpx.AsyncClient, token: str, tab: str, q: str | None = None) -> Any:
    params = {"tab": tab} | ({"q": q} if q is not None else {})
    response = await client.get(f"{ADMIN}/mentors", params=params, headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --------------------------------------------------------------------------
# Who may reach it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", READS)
async def test_no_token_is_refused(api_client: httpx.AsyncClient, path: str) -> None:
    response = await api_client.get(ADMIN + path.replace("{id}", str(uuid4())))

    assert response.status_code == 401


@pytest.mark.parametrize("path", READS)
@pytest.mark.parametrize("revoked", [False, True], ids=["no-grant", "revoked"])
async def test_a_caller_without_a_live_grant_gets_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, path: str, revoked: bool
) -> None:
    mentor = await applicant(db_engine, "guarded")
    auth_id = uuid4()
    await make_user(
        db_engine,
        auth_id,
        "nobody@example.test",
        role="super_admin" if revoked else None,
        revoked=revoked,
    )

    response = await api_client.get(
        ADMIN + path.replace("{id}", str(mentor)), headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 404


@pytest.mark.parametrize("path", READS)
async def test_every_live_grant_may_look(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, path: str
) -> None:
    mentor = await applicant(db_engine, "looked-at")
    token = await admin_token(db_engine, "limited_access")

    response = await api_client.get(
        ADMIN + path.replace("{id}", str(mentor)), headers=bearer(token)
    )

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# Who each tab holds
# --------------------------------------------------------------------------


async def test_each_mentor_is_in_exactly_one_tab(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    admin = await make_user(db_engine, uuid4(), "decider@example.test", role="super_admin")
    waiting = await applicant(db_engine, "waiting", days_waiting=3.5)
    # Saved partway through onboarding: not an application, so in no tab.
    draft = await make_public_mentor(db_engine, "draft", approved=False, listed=False)
    listed = await approved(db_engine, "listed")
    held = await approved(db_engine, "held")
    await status_event(
        db_engine, held, "unlisted", by=admin, reason="Profile photo is a logo", days_ago=2
    )
    away = await approved(db_engine, "away")
    await status_event(db_engine, away, "unlisted", by=away, reason="mentor_paused", days_ago=1)
    declined = await make_public_mentor(db_engine, "declined", approved=False, listed=False)
    await status_event(
        db_engine, declined, "declined", by=admin, reason="No LinkedIn yet", days_ago=4
    )
    await status_event(
        db_engine, declined, "unlisted", by=admin, reason="never_approved", days_ago=4
    )

    tabs = {
        tab: {row["user_id"]: row for row in await rows(api_client, token, tab)}
        for tab in ("applications", "approved", "paused", "not_approved")
    }

    assert set(tabs["applications"]) == {str(waiting)}
    assert set(tabs["approved"]) == {str(listed)}
    assert set(tabs["paused"]) == {str(held), str(away)}
    assert set(tabs["not_approved"]) == {str(declined)}
    assert str(draft) not in {user for tab in tabs.values() for user in tab}

    application = tabs["applications"][str(waiting)]
    assert application["waiting_days"] == 3
    assert application["submitted_at"] is not None
    assert tabs["approved"][str(listed)]["approved_at"] is not None
    assert tabs["approved"][str(listed)]["waiting_days"] is None

    by_admin = tabs["paused"][str(held)]
    assert (by_admin["paused_by"], by_admin["paused_reason"]) == (
        "admin",
        "Profile photo is a logo",
    )
    assert by_admin["paused_at"] is not None
    by_mentor = tabs["paused"][str(away)]
    assert (by_mentor["paused_by"], by_mentor["paused_reason"]) == ("mentor", None)

    no = tabs["not_approved"][str(declined)]
    assert (no["decline_reason"], no["paused_by"]) == ("No LinkedIn yet", None)
    assert no["decided_at"] is not None


async def test_a_deleted_mentor_is_in_no_tab(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    gone = await applicant(db_engine, "gone")
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :m"), {"m": gone})

    assert await rows(api_client, token, "applications") == []
    detail = await api_client.get(f"{ADMIN}/mentors/{gone}", headers=bearer(token))
    assert detail.status_code == 404


async def test_applications_wait_longest_first_and_q_narrows_by_name_or_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    newer = await applicant(db_engine, "newer", days_waiting=1)
    older = await applicant(db_engine, "older", days_waiting=6)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET first_name = 'Chiamaka', last_name = 'Obi' WHERE id = :m"),
            {"m": newer},
        )

    order = [row["user_id"] for row in await rows(api_client, token, "applications")]
    by_name = await rows(api_client, token, "applications", q="aMaKa ob")
    by_email = await rows(api_client, token, "applications", q="OLDER@")
    wildcard = await rows(api_client, token, "applications", q="%")

    assert order == [str(older), str(newer)]
    assert [row["user_id"] for row in by_name] == [str(newer)]
    assert [row["user_id"] for row in by_email] == [str(older)]
    assert wildcard == []


# --------------------------------------------------------------------------
# The figures
# --------------------------------------------------------------------------


async def rate(engine: AsyncEngine, mentor: UUID, attended: int, missed: int) -> None:
    for _ in range(attended):
        await add_session(engine, mentor, status="completed", attendance="attended")
    for _ in range(missed):
        await add_session(engine, mentor, status="no_show", attendance="no_show")


async def test_the_summary_counts_tabs_and_bands_show_up(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    await applicant(db_engine, "a1", days_waiting=1)
    await applicant(db_engine, "a2", days_waiting=7.2)
    strong = await approved(db_engine, "strong")
    await rate(db_engine, strong, attended=9, missed=1)  # 90
    weak = await approved(db_engine, "weak")
    await rate(db_engine, weak, attended=1, missed=2)  # 33
    await approved(db_engine, "new")  # no rate: left out of the mean
    held = await approved(db_engine, "held")
    await rate(db_engine, held, attended=0, missed=3)  # paused: not counted
    await status_event(db_engine, held, "unlisted", by=None, reason="Review")

    response = await api_client.get(f"{ADMIN}/mentors/summary", headers=bearer(token))

    assert response.status_code == 200
    assert response.json() == {
        "counts": {"applications": 2, "approved": 3, "paused": 1, "not_approved": 0},
        "applications_longest_wait_days": 7,
        # (90 + 33) / 2 = 61.5, half up.
        "approved_average_show_up": 62,
        "at_risk": 1,
    }


async def test_an_empty_platform_summarises_to_zeros_and_nulls(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)

    response = await api_client.get(f"{ADMIN}/mentors/summary", headers=bearer(token))

    assert response.json() == {
        "counts": {"applications": 0, "approved": 0, "paused": 0, "not_approved": 0},
        "applications_longest_wait_days": None,
        "approved_average_show_up": None,
        "at_risk": 0,
    }


async def test_a_row_carries_sessions_show_up_and_first_school(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    mentor = await approved(db_engine, "busy")
    await add_education(db_engine, mentor, school="University of Ibadan")
    await rate(db_engine, mentor, attended=3, missed=1)  # 75
    await add_session(db_engine, mentor, status="confirmed", attendance="pending", days_ago=-3)

    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE institutions SET country_id = (SELECT id FROM countries WHERE code = 'NG') "
                "WHERE id = (SELECT institution_id FROM education_entries WHERE user_id = :m)"
            ),
            {"m": mentor},
        )
    typed = await approved(db_engine, "typed-school")
    await add_education(db_engine, typed, school="Somewhere typed", institution=False)

    found = {row["user_id"]: row for row in await rows(api_client, token, "approved")}
    row = found[str(mentor)]

    assert found[str(typed)]["first_school"] == {"name": "Somewhere typed", "country": None}
    assert row["first_school"]["country"] == "Nigeria"
    assert row["completed_sessions"] == 3
    assert row["upcoming_sessions"] == 1
    assert (row["show_up_rate"], row["show_up_tier"]) == (75, "needs_attention")
    assert row["first_school"]["name"] == "University of Ibadan"


# --------------------------------------------------------------------------
# The drawer
# --------------------------------------------------------------------------


async def test_the_drawer_shows_every_section(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    mentor = await approved(db_engine, "drawer")
    await add_education(db_engine, mentor, school="University of Toronto")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, about_me, social_linkedin) "
                "VALUES (:m, 'I help', 'https://www.linkedin.com/in/ada-l')"
            ),
            {"m": mentor},
        )
        await conn.execute(
            text(
                "INSERT INTO availability_rules (mentor_user_id, day_of_week, start_time, "
                "end_time, timezone) VALUES (:m, 1, '18:00', '21:00', 'Africa/Lagos')"
            ),
            {"m": mentor},
        )
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :m, id FROM service_offerings ORDER BY sort_order LIMIT 1"
            ),
            {"m": mentor},
        )
    await add_session(db_engine, mentor, status="completed", attendance="attended", days_ago=6)
    mine = await add_session(db_engine, mentor, status="no_show", attendance="no_show", days_ago=5)
    theirs = await add_session(
        db_engine,
        mentor,
        status="no_show",
        attendance="attended",
        mentee_attendance="no_show",
        days_ago=4,
    )
    off = await add_session(db_engine, mentor, status="cancelled", attendance="pending", days_ago=3)
    soon = await add_session(
        db_engine, mentor, status="confirmed", attendance="pending", days_ago=-2
    )
    # Confirmed and started, not yet settled: not upcoming, matching the stats.
    started = await add_session(
        db_engine,
        mentor,
        status="confirmed",
        attendance="pending",
        starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
    )
    # Never agreed, so never a booking.
    await add_session(db_engine, mentor, status="declined", attendance="pending", days_ago=-4)

    response = await api_client.get(f"{ADMIN}/mentors/{mentor}", headers=bearer(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["about"] == "I help"
    assert body["linkedin_url"] == "https://www.linkedin.com/in/ada-l"
    assert body["timezone"] == "Africa/Lagos"
    assert body["education"][0]["school_name_raw"] == "University of Toronto"
    assert len(body["help_areas"]) == 1
    assert body["weekly_hours"][0]["day_of_week"] == 1
    # `completed` is the profile's delivered count: the mentee's no-show is not one.
    assert body["stats"] == {
        "completed": 1,
        "upcoming": 1,
        "missed_by_mentor": 1,
        "show_up_rate": 67,
        "show_up_tier": "needs_attention",
    }
    assert [(b["id"], b["outcome"]) for b in body["recent_bookings"]] == [
        (str(soon), "upcoming"),
        (str(started), "awaiting_outcome"),
        (str(off), "canceled"),
        (str(theirs), "missed_by_mentee"),
        (str(mine), "missed_by_mentor"),
    ]
    assert body["paused"] is None
    assert body["declined"] is None


async def test_the_drawer_says_why_a_mentor_is_paused_or_declined(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    admin = await make_user(db_engine, uuid4(), "reviewer@example.test", role="super_admin")
    held = await approved(db_engine, "drawer-held")
    await status_event(db_engine, held, "unlisted", by=admin, reason="Complaints")
    declined = await make_public_mentor(db_engine, "drawer-no", approved=False, listed=False)
    await status_event(db_engine, declined, "declined", by=admin, reason="Add a photo")

    paused = (await api_client.get(f"{ADMIN}/mentors/{held}", headers=bearer(token))).json()
    no = (await api_client.get(f"{ADMIN}/mentors/{declined}", headers=bearer(token))).json()

    assert paused["status"] == "paused"
    assert paused["paused"]["by"] == "admin"
    assert paused["paused"]["reason"] == "Complaints"
    assert no["status"] == "not_approved"
    assert no["declined"]["reason"] == "Add a photo"
    assert no["paused"] is None


@pytest.mark.parametrize("who", ["draft", "not-a-mentor", "nobody"])
async def test_the_drawer_is_404_outside_every_tab(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    token = await admin_token(db_engine)
    target: UUID
    if who == "draft":
        target = await make_public_mentor(db_engine, "half", approved=False, listed=False)
    elif who == "not-a-mentor":
        target = await make_user(db_engine, uuid4(), "mentee@example.test")
    else:
        target = uuid4()

    response = await api_client.get(f"{ADMIN}/mentors/{target}", headers=bearer(token))

    assert response.status_code == 404


async def test_a_deleted_mentee_keeps_the_booking_and_loses_the_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token = await admin_token(db_engine)
    mentor = await approved(db_engine, "left-behind")
    session_id = await add_session(db_engine, mentor, status="completed", attendance="attended")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET deleted_at = now() WHERE id = "
                "(SELECT mentee_id FROM sessions WHERE id = :s)"
            ),
            {"s": session_id},
        )

    body = (await api_client.get(f"{ADMIN}/mentors/{mentor}", headers=bearer(token))).json()

    (booking,) = body["recent_bookings"]
    assert booking["id"] == str(session_id)
    assert booking["mentee_first_name"] is None


# --------------------------------------------------------------------------
# /me and the public profile
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ((), None),
        (("limited_access",), "limited_access"),
        (("limited_access", "mentor_approval"), "mentor_approval"),
        (("mentor_approval", "super_admin"), "super_admin"),
    ],
)
async def test_me_names_the_strongest_live_grant(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    roles: tuple[str, ...],
    expected: str | None,
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "me@example.test")
    async with db_engine.begin() as conn:
        for role in roles:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :r)"),
                {"u": user, "r": role},
            )
        if expected != "super_admin":
            # A revoked stronger grant counts for nothing.
            await conn.execute(
                text(
                    "INSERT INTO admin_users (user_id, admin_role, revoked_at) "
                    "VALUES (:u, 'super_admin', now())"
                ),
                {"u": user},
            )

    me = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()

    assert me["admin_role"] == expected
    assert me["is_admin"] is (expected is not None)


@pytest.mark.parametrize("state", ["applicant", "paused", "declined"])
async def test_a_live_admin_reads_a_hidden_mentors_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, state: str
) -> None:
    """Q10: "View as mentee" opens what a hidden mentor's profile says; a
    mentee, and an admin whose grant was revoked, still get the 404."""
    mentor = await (applicant if state == "applicant" else approved)(db_engine, f"hidden-{state}")
    if state == "paused":
        await status_event(db_engine, mentor, "unlisted", by=None, reason="Review")
    if state == "declined":
        await status_event(db_engine, mentor, "declined", by=None, reason="No")
    admin = await admin_token(db_engine, "limited_access")
    revoked_auth = uuid4()
    await make_user(db_engine, revoked_auth, "was@example.test", role="super_admin", revoked=True)
    mentee = await make_user(db_engine, uuid4(), "mentee-view@example.test")

    as_admin = await api_client.get(f"/api/v1/mentors/{mentor}", headers=bearer(admin))
    as_revoked = await api_client.get(
        f"/api/v1/mentors/{mentor}", headers=bearer(api_token(revoked_auth))
    )
    as_mentee = await api_client.get(
        f"/api/v1/mentors/{mentor}", headers=bearer(await token_for(db_engine, mentee))
    )
    anonymous = await api_client.get(f"/api/v1/mentors/{mentor}")
    reviews_admin = await api_client.get(f"/api/v1/mentors/{mentor}/reviews", headers=bearer(admin))
    reviews_anonymous = await api_client.get(f"/api/v1/mentors/{mentor}/reviews")

    assert (reviews_admin.status_code, reviews_anonymous.status_code) == (200, 404)
    assert as_admin.status_code == 200, as_admin.text
    # The public shape: an admin is not the owner.
    assert "approval_status" not in as_admin.json()
    assert [as_revoked.status_code, as_mentee.status_code, anonymous.status_code] == [
        404,
        404,
        404,
    ]


@pytest.mark.parametrize("tab", list(AdminMentorTab))
async def test_a_tab_reads_sessions_through_their_indexes(
    db_engine: AsyncEngine, tab: AdminMentorTab
) -> None:
    """The guardrail's `EXPLAIN` on the store's own statement: every per-mentor
    figure reaches `sessions` and the status log by index, never by a scan.
    `enable_seqscan = off` asks whether a usable index exists, not what the
    planner picks over a dozen rows."""
    mentor = await approved(db_engine, f"plan-{tab}")
    await add_session(db_engine, mentor)
    statement = tab_statement(tab, now=dt.datetime.now(dt.UTC)).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )

    async with db_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            str(line) for line in (await conn.execute(text(f"EXPLAIN {statement}"))).scalars()
        )

    assert "Seq Scan on sessions" not in plan, plan
    assert "Seq Scan on mentor_status_events" not in plan, plan
    assert "Seq Scan on session_participants" not in plan, plan

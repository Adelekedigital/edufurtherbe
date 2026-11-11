"""Decision 338: the mentor activation funnel read from this database, and the
figures beside the funnels. One small cohort, every count worked out below."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_scheduling_window,
    add_session_type,
    admin_token,
    applicant,
    make_public_mentor,
    status_event,
)
from tests.integration.test_api_admin_analytics_behaviour import PostHog, app_for, get
from tests.integration.test_api_admin_analytics_tabs import person, session_row

from app.infra.storage.supabase import SupabaseStorage
from conftest import client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

NOW = dt.datetime.now(dt.UTC)
DAY = dt.timedelta(days=1)
STEPS = "approved availability_set session_type_created first_booking_received "
STEPS += "first_session_delivered"


def ago(days: float) -> dt.datetime:
    return NOW - days * DAY


async def hours(engine: AsyncEngine, mentor: UUID, *, made: float, gone: float | None) -> None:
    """Weekly hours set ``made`` days ago, deleted ``gone`` days ago (or not)."""
    await add_availability(engine, mentor)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE availability_rules SET created_at = :c, deleted_at = :d "
                "WHERE mentor_user_id = :m"
            ),
            {"m": mentor, "c": ago(made), "d": None if gone is None else ago(gone)},
        )


async def offering(engine: AsyncEngine, mentor: UUID, *, made: float, starter: bool) -> UUID:
    session_type = await add_session_type(engine, mentor)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET created_at = :c, is_starter = :s WHERE id = :t"),
            {"t": session_type, "c": ago(made), "s": starter},
        )
    return session_type


async def cohort(engine: AsyncEngine) -> dict[str, UUID]:
    """Four counted mentors and an internal one, all set up by hand:

    * Ada, approved 10 days ago: hours from before approval, her own type 2
      days after it, a request and a completed session within 30 days, a
      calendar. Every step; bookable in 2 days.
    * Bo, approved 10 days ago: hours deleted before approval, so none; his
      own type and a request. Approved and booked only.
    * Cy, approved 10 days ago: hours deleted after approval still count; only
      onboarding's type, which is not his own.
    * Ed, approved 45 days ago: his hours are his own type's windows, both set
      before approval (bookable at once); a request and a session 31 days
      after approval, too late to count.
    * Ida: everything, but an internal account, so nowhere.
    """
    names = ("ada", "bo", "cy", "ed", "ida")
    mentors = {name: await make_public_mentor(engine, name) for name in names}
    ada, bo, cy, ed, ida = mentors.values()
    for mentor in (ada, bo, cy, ida):
        await status_event(engine, mentor, "approved", by=None, at=ago(10))
    await status_event(engine, ed, "approved", by=None, at=ago(45))
    await hours(engine, ada, made=20, gone=None)
    await offering(engine, ada, made=8, starter=False)
    await hours(engine, bo, made=20, gone=12)
    await offering(engine, bo, made=20, starter=False)
    await hours(engine, cy, made=20, gone=1)
    await offering(engine, cy, made=20, starter=True)
    await add_scheduling_window(engine, eds := await offering(engine, ed, made=46, starter=False))
    await hours(engine, ida, made=20, gone=None)
    await offering(engine, ida, made=8, starter=False)
    async with engine.begin() as conn:
        mentee = await person(conn)
        await session_row(conn, ada, mentee, status="completed", created=ago(9), starts=ago(7))
        await session_row(
            conn, bo, mentee, status="pending_mentor_approval", created=ago(9), starts=ago(-2)
        )
        await session_row(conn, ed, mentee, status="completed", created=ago(14), starts=ago(14))
        await conn.execute(
            text(
                "UPDATE session_type_scheduling_windows SET created_at = :c "
                "WHERE session_type_id = :t"
            ),
            {"t": eds, "c": ago(46)},
        )
        await session_row(conn, ida, mentee, status="completed", created=ago(9), starts=ago(7))
        for mentor in (ada, ida):
            await conn.execute(
                text(
                    "INSERT INTO calendar_connections (user_id, provider, refresh_token_encrypted) "
                    "VALUES (:u, 'google', 'sealed')"
                ),
                {"u": mentor},
            )
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": ida},
        )
    return mentors


async def funnel(
    engine: AsyncEngine, storage: SupabaseStorage | None, posthog: Any, **query: str
) -> dict[str, Any]:
    token = await admin_token(engine)
    async with client_for(app_for(engine, storage, posthog)) as client:
        answer = await get(client, token, "/funnels/mentor_activation", **query)
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


def figures(body: dict[str, Any]) -> dict[str, tuple[Any, Any, str]]:
    return {f["key"]: (f["value"], f["previous"], f["unit"]) for f in body["figures"]}


async def test_mentor_activation_counts_real_rows_and_leaves_internal_mentors_out(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    await cohort(db_engine)
    posthog = PostHog(shared=2)
    body = await funnel(db_engine, api_storage, posthog, window="90d")
    steps = body["steps"]
    assert [s["key"] for s in steps] == STEPS.split()
    assert [s["layer"] for s in steps] == ["primary"] * 3 + ["secondary"] * 2
    # Approved: Ada, Bo, Cy, Ed (not Ida). Hours: Ada, Cy, Ed. Own type, of
    # those: Ada, Ed. Booked in 30 days: Ada, Bo. Delivered, of those: Ada.
    assert [s["value"] for s in steps] == [4, 3, 2, 2, 1]
    # Nothing in the previous 90 days.
    assert [s["previous"] for s in steps] == [0] * 5
    assert figures(body) == {
        # Ada 2 days, Ed 0: the median is 1.
        "days_to_bookable": (1.0, None, "days"),
        "calendar_connected": (25.0, None, "percent"),
        "profile_link_shared": (50.0, None, "percent"),
    }
    assert [f["label"] for f in body["figures"]] == [
        "Days to bookable",
        "Connected a calendar (of approved)",
        "Shared their profile link (of approved)",
    ]
    # From the database: PostHog is asked only who shared, for the window's
    # cohort (the previous one is empty, so nothing is asked for it).
    assert posthog.asked("FunnelsQuery") == 0
    assert posthog.asked("admin_analytics_profile_links") == 1
    (asked,) = (q for q in posthog.seen if q.get("name") == "admin_analytics_profile_links")
    assert len(asked["values"]["mentors"]) == 4


async def test_compare_false_nulls_every_previous(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    await cohort(db_engine)
    body = await funnel(db_engine, api_storage, PostHog(), window="90d", compare="false")
    assert all(s["previous"] is None for s in body["steps"])
    assert all(f["previous"] is None for f in body["figures"])


async def test_a_previous_window_compares_its_own_cohort(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    await cohort(db_engine)
    # 30 days: Ada, Bo and Cy now; Ed, approved 45 days ago, is the previous period's.
    body = await funnel(db_engine, api_storage, PostHog(), window="30d")
    assert [(s["value"], s["previous"]) for s in body["steps"]] == [
        (3, 1),
        (2, 1),
        (1, 1),
        (2, 0),
        (1, 0),
    ]
    assert figures(body)["days_to_bookable"] == (2.0, 0.0, "days")
    assert figures(body)["calendar_connected"] == (33.3, 0.0, "percent")


async def test_by_invite_reads_the_sign_up_source(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    mentors = await cohort(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET signup_source = 'invite' WHERE id = :u"), {"u": mentors["ada"]}
        )
    invited = await funnel(db_engine, api_storage, PostHog(), window="90d", by="invite")
    direct = await funnel(db_engine, api_storage, PostHog(), window="90d", by="direct")
    assert invited["by"] == "invite"
    assert [s["value"] for s in invited["steps"]] == [1, 1, 1, 1, 1]
    assert [s["value"] for s in direct["steps"]] == [3, 2, 1, 1, 0]


async def test_without_posthog_the_figure_is_null_and_the_funnel_answers(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    await cohort(db_engine)
    for posthog in (None, PostHog(down=True)):
        body = await funnel(db_engine, api_storage, posthog, window="90d")
        assert [s["value"] for s in body["steps"]] == [4, 3, 2, 2, 1]
        assert figures(body)["profile_link_shared"] == (None, None, "percent")
        assert figures(body)["calendar_connected"] == (25.0, None, "percent")


async def test_approved_in_the_window_is_read_in_its_zone(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    # Approved at 23:30 UTC on 15 March: already 16 March in Lagos (UTC+1).
    mentor = await make_public_mentor(db_engine, "tz")
    late = dt.datetime(2025, 3, 15, 23, 30, tzinfo=dt.UTC)
    await status_event(db_engine, mentor, "approved", by=None, at=late)
    day = {"from": "2025-03-16", "to": "2025-03-16"}
    utc = await funnel(db_engine, api_storage, PostHog(), **day)
    lagos = await funnel(db_engine, api_storage, PostHog(), **day, tz="Africa/Lagos")
    assert (utc["steps"][0]["value"], utc["steps"][0]["previous"]) == (0, 1)
    assert (lagos["steps"][0]["value"], lagos["steps"][0]["previous"]) == (1, 0)


async def test_the_other_funnels_still_read_posthog_and_application_has_its_review_days(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    # Submitted 2 days ago, approved now: 2 days in review.
    await status_event(db_engine, await applicant(db_engine, "ap"), "approved", by=None)
    token = await admin_token(db_engine)
    posthog = PostHog()
    async with client_for(app_for(db_engine, api_storage, posthog)) as client:
        bodies = {
            name: (await get(client, token, f"/funnels/{name}", window="7d")).json()
            for name in ("signup", "booking", "mentee_activation", "mentor_application")
        }
    series = [len(q["series"]) for q in posthog.seen if q["kind"] == "FunnelsQuery"]
    # Each funnel twice (this period and the previous), with its new steps.
    assert series == [3, 3, 6, 6, 3, 3, 6, 6]
    assert all(bodies[name]["figures"] == [] for name in ("signup", "booking", "mentee_activation"))
    assert bodies["mentor_application"]["figures"] == [
        {
            "key": "median_review_days",
            "label": "Median days in review",
            "value": 2.0,
            "previous": None,
            "unit": "days",
        }
    ]

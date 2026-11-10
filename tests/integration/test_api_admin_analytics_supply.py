"""The admin Analytics Supply tab (PRD 31 PR 3, decision 324).

One platform built by hand, every field asserted with its arithmetic. What
carries weight: the cohort is applications **submitted** in the window, judged
by their state now; review time is decisions **made** in the window; an
unlisting that is a side effect (a decline, a deleted account) is not one; the
places rank where mentees go unserved, five at most; and admins and demo
accounts are in no figure.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_education,
    admin_token,
    make_bookable_mentor,
    make_institution,
    make_public_mentor,
    status_event,
)
from tests.integration.test_api_admin_analytics import a_user, app_for

from app.infra.db.demo_seed import DEMO_DOMAIN
from app.infra.storage.supabase import SupabaseStorage
from conftest import client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

SUPPLY = "/api/v1/admin/analytics/supply"
#: Whole seconds, so differences between rows are exact.
T0 = dt.datetime.now(dt.UTC).replace(microsecond=0)


def ago(days: float) -> dt.datetime:
    return T0 - dt.timedelta(days=days)


async def submitted(engine: AsyncEngine, mentor: UUID, at: dt.datetime) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET submitted_at = :at WHERE user_id = :m"),
            {"m": mentor, "at": at},
        )


async def admin_grant(engine: AsyncEngine, user: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": user},
        )


async def account_deleted(engine: AsyncEngine, user: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": user})


async def profile_deleted(engine: AsyncEngine, mentor: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )


async def guides(engine: AsyncEngine, mentor: UUID, *codes: str) -> None:
    async with engine.begin() as conn:
        for position, code in enumerate(codes):
            await conn.execute(
                text(
                    "INSERT INTO mentor_guide_countries (mentor_user_id, country_id, position) "
                    "SELECT :m, id, :p FROM countries WHERE code = :c"
                ),
                {"m": mentor, "p": position, "c": code},
            )


async def mentee(
    engine: AsyncEngine,
    *,
    countries: tuple[str, ...] = (),
    schools: tuple[UUID | None, ...] = (),
    email: str | None = None,
    admin: bool = False,
    gone: bool = False,
) -> UUID:
    """A mentee with a goal: countries they aim for, schools in order (`None`
    is a school typed that the catalogue does not hold)."""
    user = await a_user(engine, email=email, admin=admin)
    async with engine.begin() as conn:
        goal = (
            await conn.execute(
                text("INSERT INTO mentee_goals (user_id) VALUES (:u) RETURNING id"), {"u": user}
            )
        ).scalar_one()
        for code in countries:
            await conn.execute(
                text(
                    "INSERT INTO mentee_goal_countries (user_id, country_id) "
                    "SELECT :u, id FROM countries WHERE code = :c"
                ),
                {"u": user, "c": code},
            )
        for position, school in enumerate(schools):
            await conn.execute(
                text(
                    "INSERT INTO mentee_goal_schools "
                    "(goal_id, institution_id, school_name_raw, position) "
                    "VALUES (:g, :i, 'Somewhere', :p)"
                ),
                {"g": goal, "i": school, "p": position},
            )
    if gone:
        await account_deleted(engine, user)
    return user


async def test_supply_counts_every_field_and_leaves_admins_and_demos_out(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    e = db_engine
    alpha = await make_institution(e, "Alpha University")
    beta = await make_institution(e, "Beta College")
    pending = await make_institution(e, "Pending Uni", status="pending_review")
    merged = await make_institution(e, "Merged Uni")
    async with e.begin() as conn:
        await conn.execute(
            text("UPDATE institutions SET status = 'merged', merged_into_id = :w WHERE id = :l"),
            {"w": alpha, "l": merged},
        )

    # --- Mentors -------------------------------------------------------------
    # m1: bookable, submitted 10 days ago, approved 8 days ago (2.0 days).
    m1 = await make_bookable_mentor(e, "m1")
    await submitted(e, m1, ago(10))
    await status_event(e, m1, "approved", by=None, at=ago(8))
    await guides(e, m1, "GB", "DE")
    await add_education(e, m1, institution_id=alpha)
    await add_education(e, m1, institution_id=alpha, course="Physics")  # still one mentor
    # m2: approved and listed, no offering so not bookable; 3.5 days to approve.
    m2 = await make_public_mentor(e, "m2")
    await submitted(e, m2, ago(5))
    await status_event(e, m2, "approved", by=None, at=ago(1.5))
    await guides(e, m2, "CA")  # not taking bookings: CA keeps 0 mentors
    await add_education(e, m2, institution_id=beta)  # likewise for Beta
    # m3: approved, then paused 2 days ago (an unlisting that counts).
    m3 = await make_public_mentor(e, "m3")
    await submitted(e, m3, ago(3))
    await status_event(e, m3, "unlisted", by=m3, reason="mentor_paused", at=ago(2))
    # m4: declined after 5.0 days; its `never_approved` unlisting is the decline.
    m4 = await make_public_mentor(e, "m4", approved=False, listed=False)
    await submitted(e, m4, ago(20))
    await status_event(e, m4, "declined", by=None, reason="Thin", at=ago(15))
    await status_event(e, m4, "unlisted", by=None, reason="never_approved", at=ago(15))
    # m5: pending; an admin unlisting with no reason still counts.
    m5 = await make_public_mentor(e, "m5", approved=False, listed=False)
    await submitted(e, m5, ago(1))
    await status_event(e, m5, "unlisted", by=None, at=ago(0.5))
    # m6: submitted before the window (not in the cohort), approved inside it
    # after 12.0 days; bookable, guides four countries, a deleted school entry.
    m6 = await make_bookable_mentor(e, "m6")
    await submitted(e, m6, ago(40))
    await status_event(e, m6, "approved", by=None, at=ago(28))
    await guides(e, m6, "GB", "FR", "NG", "IE")
    await add_education(e, m6, institution_id=alpha, deleted=True)
    # m8: a decision in the window with no submission: skipped.
    m8 = await make_public_mentor(e, "m8")
    await status_event(e, m8, "approved", by=None, at=ago(3))
    # m9: everything before the window.
    m9 = await make_public_mentor(e, "m9")
    await submitted(e, m9, ago(60))
    await status_event(e, m9, "approved", by=None, at=ago(45))
    await status_event(e, m9, "unlisted", by=m9, reason="mentor_paused", at=ago(40))
    # Deleted profile and deleted account: out of the cohort. The account's
    # `account_deleted` unlisting is a side effect, not counted.
    profile_gone = await make_public_mentor(e, "pg")
    await submitted(e, profile_gone, ago(2))
    await profile_deleted(e, profile_gone)
    account_gone = await make_public_mentor(e, "ag")
    await submitted(e, account_gone, ago(2))
    await status_event(e, account_gone, "unlisted", by=None, reason="account_deleted", at=ago(1))
    await account_deleted(e, account_gone)
    # An admin who mentors: bookable, in the window, approved after 1.0 day,
    # guides the US and studied at Beta. In no figure.
    staff = await make_bookable_mentor(e, "staff")
    await admin_grant(e, staff)
    await submitted(e, staff, ago(2))
    await status_event(e, staff, "approved", by=None, at=ago(1))
    await guides(e, staff, "US")
    await add_education(e, staff, institution_id=beta)
    # A demo mentor: in the window, approved after 1.0 day, then paused.
    demo = await make_public_mentor(e, "demo", email=f"demo-{uuid4()}@{DEMO_DOMAIN}")
    await submitted(e, demo, ago(2))
    await status_event(e, demo, "approved", by=None, at=ago(1))
    await status_event(e, demo, "unlisted", by=demo, reason="mentor_paused", at=ago(1))

    # --- Mentees -------------------------------------------------------------
    await mentee(e, countries=("GB", "US", "DE", "FR"), schools=(alpha, pending, merged, alpha))
    await mentee(e, countries=("GB", "US", "DE", "FR"), schools=(alpha, beta))
    await mentee(e, countries=("GB", "US"), schools=(beta, None))
    await mentee(e, countries=("GB", "CA", "NG"))
    # Left out: an admin's, a demo account's and a deleted account's goals.
    await mentee(e, countries=("CA",), schools=(alpha,), admin=True)
    await mentee(e, countries=("CA",), schools=(alpha,), email=f"d-{uuid4()}@{DEMO_DOMAIN}")
    await mentee(e, countries=("CA",), schools=(beta,), gone=True)

    token = await admin_token(e)
    async with client_for(app_for(e, api_storage)) as client:
        headers = {"Authorization": f"Bearer {token}"}
        answer = await client.get(SUPPLY, params={"window": "30d"}, headers=headers)
        quiet = await client.get(
            SUPPLY, params={"window": "30d", "compare": "false"}, headers=headers
        )

    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "private, no-store"
    body = answer.json()
    # compare=false is accepted and changes nothing: the tab has no `previous`
    # (a quiet alert is raised at each request's own now, so it is set aside).
    assert quiet.status_code == 200
    assert quiet.json() | {"alerts": None} == body | {"alerts": None}

    # Submitted in the window, live, counted: m1, m2, m3, m4, m5 = 5.
    # Approved now: m1, m2, m3 (m4 declined, m5 pending) = 3.
    # Listed now: m1, m2 (m3 paused) = 2. Bookable now: m1 = 1.
    assert body["cohort"] == {"applied": 5, "approved": 3, "listed": 2, "bookable": 1}
    # Decisions in the window: m1 2.0, m2 3.5, m4 5.0, m6 12.0 days (m8 has no
    # submission, m9 is before the window, staff and demo are left out).
    # Median of four: (3.5 + 5.0) / 2 = 4.25, half up to 4.3.
    assert body["median_review_days"] == 4.3
    # m3 paused and m5 with no reason = 2 (m4's decline, the deleted account,
    # the demo's pause and m9's old pause are not counted).
    assert body["unlisted"] == 2
    # Mentees per country: GB 4, US 3, DE 2, FR 2, CA 1, NG 1 (IE has none).
    # Bookable mentors: GB m1+m6 = 2, DE m1, FR m6, NG m6 = 1; CA (m2 is not
    # bookable) and US (the admin) 0. Zero first by name: Canada, United
    # States; then DE, FR and GB all 2 per mentor, so by name (France before
    # Germany, unlike their codes); NG 1 is sixth.
    assert body["by_country"] == [
        {"key": "CA", "name": "Canada", "mentees": 1, "bookable_mentors": 0},
        {"key": "US", "name": "United States", "mentees": 3, "bookable_mentors": 0},
        {"key": "FR", "name": "France", "mentees": 2, "bookable_mentors": 1},
        {"key": "DE", "name": "Germany", "mentees": 2, "bookable_mentors": 1},
        {"key": "GB", "name": "United Kingdom", "mentees": 4, "bookable_mentors": 2},
    ]
    # Beta: 2 mentees, 0 bookable mentors (m2 is not bookable, the admin is
    # left out). Alpha: 2 mentees (the first lists it twice), m1 with two
    # entries there (m6's is deleted) = 1. Pending and merged schools, and
    # the typed one, are not places.
    assert body["by_school"] == [
        {"key": str(beta), "name": "Beta College", "mentees": 2, "bookable_mentors": 0},
        {"key": str(alpha), "name": "Alpha University", "mentees": 2, "bookable_mentors": 1},
    ]


async def test_an_empty_platform_reads_zeros_and_no_median(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    async with client_for(app_for(db_engine, api_storage)) as client:
        answer = await client.get(SUPPLY, headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["cohort"] == {"applied": 0, "approved": 0, "listed": 0, "bookable": 0}
    assert body["median_review_days"] is None
    assert body["unlisted"] == 0
    assert body["by_country"] == []
    assert body["by_school"] == []
    assert set(body) == {
        "window",
        "alerts",
        "cohort",
        "median_review_days",
        "unlisted",
        "by_country",
        "by_school",
    }


async def test_switched_off_the_supply_tab_is_a_404(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    token = await admin_token(db_engine)
    async with client_for(app_for(db_engine, api_storage, on=False)) as client:
        answer = await client.get(SUPPLY, headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 404

"""The admin Analytics Growth tab (PRD 31 PR 4).

One small platform, built by hand, whose every figure is worked out in the
comments. The window is `7d`: today and the six days before; the previous
period is the seven days before that. Left out everywhere: admins and
`@demo.edufurther.test` accounts.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_admin_analytics_tabs import read

from app.infra.storage.supabase import SupabaseStorage
from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

NOW = dt.datetime.now(dt.UTC)

#: Every sign-up the platform holds, by days ago (see `platform`).
SIGNUP_DAYS_AGO = (1, 2, 3, 4, 5, 10, 70, 200)


def ago(days: float) -> dt.datetime:
    return NOW - dt.timedelta(days=days)


async def user(
    conn: Any,
    *,
    days_ago: float = 0,
    role: str = "mentee",
    first: str | None = None,
    last: str | None = None,
    email: str | None = None,
    migrated: bool = False,
    deleted: bool = False,
    admin: bool = False,
) -> UUID:
    made = ago(days_ago)
    uid = await add_user(
        conn,
        email or f"{uuid4()}@example.test",
        primary_role=role,
        timezone="UTC",
        first_name=first,
        last_name=last,
        legacy_bubble_id=f"legacy-{uuid4()}" if migrated else None,
        deleted_at=made if deleted else None,
        created_at=made,
    )
    if admin:
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": uid},
        )
    return uid


async def invite(
    conn: Any,
    referrer: UUID,
    *,
    days_ago: float,
    invitee: UUID | None = None,
    joined: bool = False,
    qualified: bool = False,
) -> None:
    at = ago(days_ago)
    await conn.execute(
        text(
            "INSERT INTO referrals (referrer_id, code, invitee_user_id, created_at, "
            "signed_up_at, qualified_at) VALUES (:r, :c, :i, :at, :s, :q)"
        ),
        {
            "r": referrer,
            "c": uuid4().hex,
            "i": invitee,
            "at": at,
            "s": at if joined else None,
            "q": at if qualified else None,
        },
    )


async def session_type(
    conn: Any, mentor: UUID, *, offerings: tuple[str, ...], stages: tuple[str, ...]
) -> UUID:
    type_id = (
        await conn.execute(
            text("INSERT INTO session_types (mentor_user_id, name) VALUES (:m, :n) RETURNING id"),
            {"m": mentor, "n": f"Type {uuid4().hex[:6]}"},
        )
    ).scalar_one()
    for position, slug in enumerate(offerings):
        await conn.execute(
            text(
                "INSERT INTO session_type_offerings (session_type_id, service_offering_id, "
                "position) SELECT :t, id, :p FROM service_offerings WHERE slug = :s"
            ),
            {"t": type_id, "p": position, "s": slug},
        )
    for position, stage in enumerate(stages):
        await conn.execute(
            text(
                "INSERT INTO session_type_stages (session_type_id, stage, position) "
                "VALUES (:t, :s, :p)"
            ),
            {"t": type_id, "s": stage, "p": position},
        )
    return type_id


async def session_row(
    conn: Any,
    mentor: UUID,
    mentee: UUID,
    *,
    status: str,
    starts: dt.datetime,
    type_id: UUID | None,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, status, "
            "session_type_id) VALUES (:m, :e, :s, 30, :st, :t)"
        ),
        {"m": mentor, "e": mentee, "s": starts, "st": status, "t": type_id},
    )


@pytest_asyncio.fixture
async def platform(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        # Sign-ups, by days ago: Zara 1, Bo 2, Mia (a mentor) 3, Cy 4, Dee 5 in
        # the window; Old 10 in the previous period; Far 70, in neither; Gone
        # 200, deleted (still counted), before the six months.
        zara = await user(conn, days_ago=1, first="Zara", last="Obi")
        # Padded, no last name: joined as "Bo".
        bo = await user(conn, days_ago=2, first=" Bo ", last=None)
        mia = await user(conn, days_ago=3, role="mentor", first="Mia")
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'M')"), {"u": mia}
        )
        cy = await user(conn, days_ago=4)
        dee = await user(conn, days_ago=5)
        await user(conn, days_ago=10)
        await user(conn, days_ago=70)
        gone = await user(conn, days_ago=200, first="Aaron", deleted=True)
        # Not sign-ups: a migrated account; an admin and a demo account.
        await user(conn, migrated=True)
        admin = await user(conn, admin=True)
        await user(conn, email=f"{uuid4()}@demo.edufurther.test")
        # Migrated but a demo account: not in `migrated.count`.
        await user(conn, email=f"{uuid4()}@demo.edufurther.test", migrated=True)

        # Invites in the window. Zara: Bo (joined, qualified), Mia (joined),
        # one nobody answered. Bo: Cy and Dee (both joined). Gone, deleted:
        # three joined, more than anyone, but not a live inviter.
        await invite(conn, zara, days_ago=2, invitee=bo, joined=True, qualified=True)
        await invite(conn, zara, days_ago=3, invitee=mia, joined=True)
        await invite(conn, zara, days_ago=1)
        await invite(conn, bo, days_ago=4, invitee=cy, joined=True)
        await invite(conn, bo, days_ago=5, invitee=dee, joined=True)
        for _ in range(3):
            await invite(conn, gone, days_ago=1, joined=True)
        # Left out: an admin's three joined invites, and Zara's invite before
        # the window.
        await invite(conn, admin, days_ago=1, joined=True, qualified=True)
        for _ in range(2):
            await invite(conn, admin, days_ago=1, joined=True)
        await invite(conn, zara, days_ago=10, joined=True, qualified=True)

        # T1 covers two topics and two stages; T2 one topic and no stage.
        t1 = await session_type(
            conn,
            mia,
            offerings=("test-preparation", "school-selection"),
            stages=("exploring", "applying"),
        )
        t2 = await session_type(conn, mia, offerings=("document-preparation",), stages=())
        # Completed in the window: S1 (T1), S2 (T2), S3 (no type).
        await session_row(conn, mia, bo, status="completed", starts=ago(1), type_id=t1)
        await session_row(conn, mia, bo, status="completed", starts=ago(2), type_id=t2)
        await session_row(conn, mia, zara, status="completed", starts=ago(3), type_id=None)
        # Not counted: Cy's request is not completed; the admin's session is
        # left out; Bo's earlier one is before the window.
        await session_row(
            conn, mia, cy, status="pending_mentor_approval", starts=ago(-2), type_id=t1
        )
        await session_row(conn, admin, zara, status="completed", starts=ago(1), type_id=t1)
        await session_row(conn, mia, bo, status="completed", starts=ago(10), type_id=t1)


def last_six_months() -> list[str]:
    year, month = NOW.year, NOW.month
    labels = []
    for _ in range(6):
        labels.insert(0, f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return labels


@pytest.mark.usefixtures("platform")
async def test_growth_tab_counts_every_figure(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    body = await read(db_engine, api_storage, "growth", window="7d")
    # Zara, Bo, Cy, Dee (mentees) and Mia (a mentor) against Old.
    assert body["signups"] == {
        "all": {"value": 5, "previous": 1},
        "mentees": {"value": 4, "previous": 1},
        "mentors": {"value": 1, "previous": 0},
    }
    assert body["by_source"] == []
    assert body["by_channel"] == []
    assert body["invites"] == {
        # Zara 3, Bo 2, Gone 3.
        "invited": 8,
        # Zara 2, Bo 2, Gone 3.
        "joined": 7,
        # Bo, invited by Zara.
        "qualified": 1,
        # Bo (completed) and Cy (a pending request).
        "first_booking": 2,
        # Bo.
        "first_completed": 1,
        "cost_per_completed": None,
        # Zara and Bo both joined 2 (Gone is deleted, the admin left out):
        # "Bo" sorts before "Zara Obi", though Zara's account is older.
        "top_inviter": {"name": "Bo", "joined": 2},
    }
    # Oldest first, ending this month: each month counts the sign-ups whose
    # day falls in it (Gone, 200 days ago, in none; Far, 70, in an early one).
    signed_up = [ago(days).strftime("%Y-%m") for days in SIGNUP_DAYS_AGO]
    assert body["months"] == [
        {"month": label, "count": signed_up.count(label)} for label in last_six_months()
    ]
    # The plain migrated account; the migrated demo account is left out.
    assert body["migrated"] == {"count": 1, "on": None}
    # S1, S2 and S3.
    assert body["completed_total"] == 3
    assert body["by_topic"] == [
        # S1 counts under both of T1's topics; S2 under T2's; S3 has no type.
        {"key": "test-preparation", "name": "Test preparation", "count": 1},
        {"key": "document-preparation", "name": "Document preparation", "count": 1},
        {"key": "school-selection", "name": "School selection", "count": 1},
        {"key": "program-selection", "name": "Program selection", "count": 0},
        {"key": "scholarships-financial-aid", "name": "Scholarships and financial aid", "count": 0},
        {"key": "interview-preparation", "name": "Interview preparation", "count": 0},
    ]
    assert body["by_stage"] == [
        # S1 under both of T1's stages; S2's type has none and S3 has no type.
        {"key": "exploring", "count": 1},
        {"key": "researching", "count": 0},
        {"key": "preparing_documents", "count": 0},
        {"key": "applying", "count": 1},
        {"key": "interview_prep", "count": 0},
        {"key": "awaiting_decision", "count": 0},
        {"key": "not_set", "count": 2},
    ]


@pytest.mark.usefixtures("platform")
async def test_compare_false_nulls_every_previous(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    body = await read(db_engine, api_storage, "growth", window="7d", compare="false")
    assert body["signups"] == {
        "all": {"value": 5, "previous": None},
        "mentees": {"value": 4, "previous": None},
        "mentors": {"value": 1, "previous": None},
    }


async def test_an_empty_platform_reads_zeros_and_nulls(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    body = await read(db_engine, api_storage, "growth", window="7d")
    assert body["signups"]["all"] == {"value": 0, "previous": 0}
    assert body["invites"] == {
        "invited": 0,
        "joined": 0,
        "qualified": 0,
        "first_booking": 0,
        "first_completed": 0,
        "cost_per_completed": None,
        "top_inviter": None,
    }
    assert body["months"] == [{"month": label, "count": 0} for label in last_six_months()]
    assert body["migrated"] is None
    assert body["completed_total"] == 0
    assert [topic["count"] for topic in body["by_topic"]] == [0] * 6
    assert body["by_stage"][-1] == {"key": "not_set", "count": 0}


async def test_an_invite_nobody_answered_has_no_top_inviter(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    async with db_engine.begin() as conn:
        await invite(conn, await user(conn, first="Ann"), days_ago=1)
    body = await read(db_engine, api_storage, "growth", window="7d")
    assert body["invites"]["invited"] == 1
    assert body["invites"]["joined"] == 0
    assert body["invites"]["top_inviter"] is None

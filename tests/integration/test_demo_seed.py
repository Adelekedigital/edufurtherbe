"""Demo mentors: seeded through ordinary rows, visible through the real API, and
removed without touching anybody else.

Removal is the test that matters. It deletes by the demo email suffix, against
foreign keys that `RESTRICT` for real users, so a wrong order fails loudly —
and a wrong filter would delete a real mentor, which is what the untouched
mentor here is for.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import add_session, make_bookable_mentor

from app.infra.db.calendar_store import NullFreeBusy
from app.infra.db.demo_seed import (
    DEMO_DOMAIN,
    DemoMentor,
    DemoQuestion,
    DemoSessionType,
    RealHistoryError,
    apply_demo_session_types,
    create_demo_mentor,
    remove_demo,
)
from app.infra.db.next_available_store import refresh_next_available

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = dt.datetime.now(dt.UTC)

OPEN = DemoMentor(
    key="demo-open",
    first_name="Sofia",
    last_name="Marin",
    headline="Chevening scholar, now at Oxford",
    about_me="I help applicants tell their story.",
    timezone="Africa/Lagos",
    study_country="GB",
    origin_country="NG",
    degree_level="masters",
    course="Public Policy",
    school="University of Oxford",
    offerings=("test-preparation", "interview-preparation"),
    completed_sessions=4,
    ratings=(5, 4),
)
BLOCKED = DemoMentor(
    key="demo-blocked",
    first_name="Daniel",
    last_name="Reyes",
    headline="PhD, Mathematics",
    about_me="Fully booked this quarter.",
    timezone="Europe/London",
    study_country="US",
    origin_country="GH",
    degree_level="doctorate",
    course="Mathematics",
    school="MIT",
    offerings=("scholarships-financial-aid",),
    completed_sessions=0,
    open=False,
)


async def seed(engine: AsyncEngine, *mentors: DemoMentor) -> None:
    async with AsyncSession(engine) as session:
        for mentor in mentors:
            await create_demo_mentor(session, mentor, now=NOW)
        await session.commit()
        await refresh_next_available(
            session, max_age=dt.timedelta(minutes=5), reader=NullFreeBusy()
        )


async def demo_users(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM users WHERE email LIKE :s"),
                    {"s": f"%@{DEMO_DOMAIN}"},
                )
            ).scalar_one()
        )


async def test_seeded_mentors_show_on_the_explore_page(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await seed(db_engine, OPEN, BLOCKED)

    cards = {
        row["first_name"]: row for row in (await api_client.get("/api/v1/mentors")).json()["data"]
    }

    sofia = cards["Sofia"]
    assert sofia["completed_sessions"] == 4
    assert sofia["review_count"] == 2
    assert sofia["session_value"] == pytest.approx(4.5)
    assert {o["slug"] for o in sofia["offerings"]} == {"test-preparation", "interview-preparation"}
    assert sofia["institution"] == "University of Oxford"
    assert sofia["next_available_state"] == "open"
    assert cards["Daniel"]["next_available_state"] == "none"
    assert cards["Daniel"]["review_count"] == 0


async def test_removal_leaves_nobody_else_touched(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    real = await make_bookable_mentor(db_engine, "real-mentor")
    await add_session(db_engine, real, days_ago=2)
    await seed(db_engine, OPEN, BLOCKED)
    assert await demo_users(db_engine) > 0

    async with AsyncSession(db_engine) as session:
        removed = await remove_demo(session)
        await session.commit()

    assert removed >= 2
    assert await demo_users(db_engine) == 0
    listed = [row["id"] for row in (await api_client.get("/api/v1/mentors")).json()["data"]]
    assert listed == [str(real)]


async def test_seeding_twice_after_removal_is_clean(db_engine: AsyncEngine) -> None:
    """The script removes before it seeds, so re-running replaces the set."""
    await seed(db_engine, OPEN)
    async with AsyncSession(db_engine) as session:
        await remove_demo(session)
        await session.commit()
    await seed(db_engine, OPEN)

    assert await demo_users(db_engine) == 1 + len(OPEN.ratings)


async def test_removal_refuses_when_a_real_user_booked_a_demo_mentor(
    db_engine: AsyncEngine,
) -> None:
    """A tester's booking with a demo mentor is real history. Removal must not
    erase it, and must not half-run into the credit ledger's RESTRICT key."""
    await seed(db_engine, OPEN)
    async with db_engine.begin() as conn:
        demo_mentor = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e"), {"e": f"demo-open@{DEMO_DOMAIN}"}
            )
        ).scalar_one()
    real_mentee_session = await add_session(db_engine, demo_mentor, days_ago=-3)
    before = await demo_users(db_engine)

    async with AsyncSession(db_engine) as session:
        with pytest.raises(RealHistoryError):
            await remove_demo(session)

    assert await demo_users(db_engine) == before
    async with db_engine.begin() as conn:
        kept = await conn.execute(
            text("SELECT count(*) FROM sessions WHERE id = :s"), {"s": real_mentee_session}
        )
        assert kept.scalar_one() == 1


# --------------------------------------------------------------------------
# Session types: several per mentor, each with what the frontend displays
# --------------------------------------------------------------------------

MOCK = DemoSessionType(
    name="Mock interview with feedback",
    description="A full practice interview, then feedback on every answer.",
    offering="interview-preparation",
    duration=60,
    notice=2880,
    stage="post_submission",
    questions=(
        DemoQuestion("Which programme or scholarship is the interview for?", required=True),
        DemoQuestion("When is your interview?", required=False),
    ),
)
INTRO = DemoSessionType(
    name="Intro call",
    description="Twenty minutes to see whether we are a good fit.",
    offering=None,
    duration=20,
    notice=1440,
    stage=None,
    questions=(DemoQuestion("What would you like to get out of this call?", required=True),),
)


async def session_types_of(engine: AsyncEngine, user: object) -> dict[str, dict[str, object]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT t.id, t.name, t.description, t.application_stage, c.duration_minutes, "
                "       c.min_notice_minutes, o.slug AS offering, "
                "       (SELECT count(*) FROM session_type_questions q "
                "         WHERE q.session_type_id = t.id AND q.deleted_at IS NULL) AS questions "
                "FROM session_types t "
                "JOIN session_type_booking_configs c ON c.session_type_id = t.id "
                "LEFT JOIN service_offerings o ON o.id = t.service_offering_id "
                "WHERE t.mentor_user_id = :u AND t.deleted_at IS NULL"
            ),
            {"u": user},
        )
        return {row.name: dict(row._mapping) for row in rows}


async def demo_user(engine: AsyncEngine, key: str) -> object:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e"), {"e": f"{key}@{DEMO_DOMAIN}"}
            )
        ).scalar_one()


async def test_a_seeded_mentor_offers_every_session_type_given(db_engine: AsyncEngine) -> None:
    mentor = replace(OPEN, key="demo-types", session_types=(MOCK, INTRO))
    await seed(db_engine, mentor)

    types = await session_types_of(db_engine, await demo_user(db_engine, "demo-types"))

    assert set(types) == {"Mock interview with feedback", "Intro call"}
    mock = types["Mock interview with feedback"]
    assert (mock["duration_minutes"], mock["min_notice_minutes"]) == (60, 2880)
    assert (mock["offering"], mock["application_stage"]) == (
        "interview-preparation",
        "post_submission",
    )
    assert mock["description"]
    assert mock["questions"] == 2
    assert types["Intro call"]["offering"] is None


async def test_upgrading_keeps_the_legacy_type_and_its_history(db_engine: AsyncEngine) -> None:
    """A mentor seeded before this change has one '1:1 mentorship' type that
    their demo sessions point at. The upgrade rewrites that row into the first
    type rather than deleting it, and adds the rest."""
    legacy = replace(OPEN, key="demo-legacy")
    await seed(db_engine, legacy)
    user = await demo_user(db_engine, "demo-legacy")
    (before,) = (await session_types_of(db_engine, user)).values()

    async with AsyncSession(db_engine) as session:
        await apply_demo_session_types(session, user, (MOCK, INTRO))
        await session.commit()
    after = await session_types_of(db_engine, user)

    assert set(after) == {"Mock interview with feedback", "Intro call"}
    assert after["Mock interview with feedback"]["id"] == before["id"]
    async with db_engine.begin() as conn:
        orphaned = await conn.execute(
            text(
                "SELECT count(*) FROM sessions s WHERE s.mentor_id = :u AND s.session_type_id "
                "NOT IN (SELECT id FROM session_types WHERE mentor_user_id = :u)"
            ),
            {"u": user},
        )
    assert orphaned.scalar_one() == 0


async def test_upgrading_twice_changes_nothing(db_engine: AsyncEngine) -> None:
    await seed(db_engine, replace(OPEN, key="demo-twice"))
    user = await demo_user(db_engine, "demo-twice")

    for _ in range(2):
        async with AsyncSession(db_engine) as session:
            await apply_demo_session_types(session, user, (MOCK, INTRO))
            await session.commit()

    types = await session_types_of(db_engine, user)
    assert len(types) == 2
    assert types["Mock interview with feedback"]["questions"] == 2
    assert types["Intro call"]["questions"] == 1


async def test_the_session_types_are_public_on_the_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await seed(db_engine, replace(OPEN, key="demo-public", session_types=(MOCK, INTRO)))
    user = await demo_user(db_engine, "demo-public")

    body = (await api_client.get(f"/api/v1/users/{user}/session-types")).json()

    assert sorted(t["name"] for t in body["data"]) == ["Intro call", "Mock interview with feedback"]

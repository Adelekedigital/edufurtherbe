"""Demo mentors: seeded through ordinary rows, visible through the real API, and
removed without touching anybody else.

Removal is the test that matters. It deletes by the demo email suffix, against
foreign keys that `RESTRICT` for real users, so a wrong order fails loudly —
and a wrong filter would delete a real mentor, which is what the untouched
mentor here is for.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import add_session, make_bookable_mentor

from app.infra.db.calendar_store import NullFreeBusy
from app.infra.db.demo_seed import (
    DEMO_DOMAIN,
    DemoMentor,
    RealHistoryError,
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

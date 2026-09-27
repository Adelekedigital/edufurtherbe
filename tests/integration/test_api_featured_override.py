"""An admin chooses the featured mentor for a week (settled decision #188).

The automatic rotation stays the default and needs nobody (#177). An admin
may override a week — this one or up to eight ahead — with a mentor who is
bookable now. The override **counts as that mentor's rotation turn**, and a
mentor the rotation had already picked for that week **gets their turn back**.
Only mentor-approval admins (and super admins) may do it.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import make_bookable_mentor, make_public_mentor
from tests.integration.test_api_admin import make_user

from app.domain.featured import week_start
from app.infra.db.featured_store import current_featured
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

PUBLIC = "/api/v1/featured-mentor"
ADMIN = "/api/v1/admin/featured-mentor"
SCHEDULE = "/api/v1/admin/featured-mentors"


def this_week() -> dt.date:
    return week_start(dt.datetime.now(dt.UTC))


async def an_admin(engine: AsyncEngine, role: str | None = "mentor_approval") -> dict[str, str]:
    auth_id = uuid4()
    await make_user(engine, auth_id, f"admin-{auth_id.hex[:8]}@example.com", role=role)
    return bearer(api_token(auth_id))


async def rows(engine: AsyncEngine) -> list[tuple[dt.date, UUID, str, int]]:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT week_start, mentor_user_id, source, cycle FROM featured_mentors "
                "ORDER BY created_at, id"
            )
        )
        return [(r.week_start, r.mentor_user_id, r.source, r.cycle) for r in result]


async def featured_now(engine: AsyncEngine) -> UUID | None:
    async with AsyncSession(engine) as session:
        return await current_featured(session, now=dt.datetime.now(dt.UTC))


async def test_an_admin_features_a_mentor_this_week(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    chosen = await make_bookable_mentor(db_engine, "ov-chosen")
    await make_bookable_mentor(db_engine, "ov-other")

    response = await api_client.put(
        f"{ADMIN}/{this_week()}", json={"mentor_id": str(chosen)}, headers=await an_admin(db_engine)
    )
    public = (await api_client.get(PUBLIC)).json()

    assert response.status_code == 200
    assert public["id"] == str(chosen)


async def test_a_scheduled_week_is_used_when_it_arrives(db_engine: AsyncEngine) -> None:
    """Asked of the store with a clock, so "next week" can arrive."""
    from app.infra.db.featured_store import set_featured

    await make_bookable_mentor(db_engine, "ov-future-other")
    chosen = await make_bookable_mentor(db_engine, "ov-future")
    admin = await make_user(db_engine, uuid4(), "admin-future@example.com", role="mentor_approval")
    now = dt.datetime.now(dt.UTC)
    next_week = week_start(now) + dt.timedelta(days=7)

    async with AsyncSession(db_engine) as session:
        await set_featured(session, next_week, chosen, admin, now=now)
    async with AsyncSession(db_engine) as session:
        picked = await current_featured(
            session, now=dt.datetime.combine(next_week, dt.time(9), tzinfo=dt.UTC)
        )

    assert picked == chosen


async def test_the_override_counts_as_the_mentors_turn(db_engine: AsyncEngine) -> None:
    """Featured by an admin this week, they are not picked again until every
    other bookable mentor has had a turn."""
    from app.infra.db.featured_store import set_featured

    chosen = await make_bookable_mentor(db_engine, "turn-chosen")
    others = {await make_bookable_mentor(db_engine, f"turn-{n}") for n in range(3)}
    admin = await make_user(db_engine, uuid4(), "admin-turn@example.com", role="mentor_approval")
    now = dt.datetime.now(dt.UTC)
    async with AsyncSession(db_engine) as session:
        await set_featured(session, week_start(now), chosen, admin, now=now)

    picked = set()
    for weeks in range(1, 4):
        async with AsyncSession(db_engine) as session:
            picked.add(await current_featured(session, now=now + dt.timedelta(weeks=weeks)))

    assert picked == others


async def test_a_bumped_automatic_pick_gets_their_turn_back(db_engine: AsyncEngine) -> None:
    from app.infra.db.featured_store import set_featured

    for n in range(3):
        await make_bookable_mentor(db_engine, f"bump-{n}")
    now = dt.datetime.now(dt.UTC)
    bumped = await featured_now(db_engine)
    chosen = next(
        mentor
        for mentor in [await make_bookable_mentor(db_engine, "bump-chosen")]
        if mentor != bumped
    )
    admin = await make_user(db_engine, uuid4(), "admin-bump@example.com", role="mentor_approval")
    async with AsyncSession(db_engine) as session:
        await set_featured(session, week_start(now), chosen, admin, now=now)

    this_week_rows = [r for r in await rows(db_engine) if r[0] == week_start(now)]
    assert [(r[1], r[2]) for r in this_week_rows] == [(chosen, "admin")]
    # Still in the rotation: picked in one of the next weeks.
    later = set()
    for weeks in range(1, 4):
        async with AsyncSession(db_engine) as session:
            later.add(await current_featured(session, now=now + dt.timedelta(weeks=weeks)))
    assert bumped in later


async def test_a_mentor_who_cannot_be_booked_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    hidden = await make_public_mentor(db_engine, "ov-unbookable")  # no offering, no hours

    response = await api_client.put(
        f"{ADMIN}/{this_week()}", json={"mentor_id": str(hidden)}, headers=await an_admin(db_engine)
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("offset", "why"),
    [(-7, "a past week"), (1, "not a Monday"), (9 * 7, "beyond eight weeks")],
)
async def test_a_week_outside_the_window_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, offset: int, why: str
) -> None:
    mentor = await make_bookable_mentor(db_engine, f"window-{offset}")
    week = this_week() + dt.timedelta(days=offset)

    response = await api_client.put(
        f"{ADMIN}/{week}", json={"mentor_id": str(mentor)}, headers=await an_admin(db_engine)
    )

    assert response.status_code == 422, why


async def test_eight_weeks_ahead_is_allowed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "window-edge")
    week = this_week() + dt.timedelta(weeks=8)

    response = await api_client.put(
        f"{ADMIN}/{week}", json={"mentor_id": str(mentor)}, headers=await an_admin(db_engine)
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("role", "status"),
    [("mentor_approval", 200), ("super_admin", 200), ("limited_access", 404), (None, 404)],
)
async def test_only_mentor_admins_may_choose(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, role: str | None, status: int
) -> None:
    mentor = await make_bookable_mentor(db_engine, f"role-{role or 'none'}")

    response = await api_client.put(
        f"{ADMIN}/{this_week()}",
        json={"mentor_id": str(mentor)},
        headers=await an_admin(db_engine, role),
    )

    assert response.status_code == status


async def test_removing_the_override_resumes_the_rotation(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    chosen = await make_bookable_mentor(db_engine, "rm-chosen")
    other = await make_bookable_mentor(db_engine, "rm-other")
    headers = await an_admin(db_engine)
    await api_client.put(f"{ADMIN}/{this_week()}", json={"mentor_id": str(chosen)}, headers=headers)

    removed = await api_client.delete(f"{ADMIN}/{this_week()}", headers=headers)
    public = (await api_client.get(PUBLIC)).json()
    again = await api_client.delete(f"{ADMIN}/{this_week()}", headers=headers)

    assert removed.status_code == 204
    assert public["id"] in {str(chosen), str(other)}
    assert [r[2] for r in await rows(db_engine)] == ["automatic"]
    assert again.status_code == 404


async def test_an_override_whose_mentor_pauses_falls_back_to_the_rotation(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The page is never empty because an admin forgot: a paused choice is
    skipped, and the rotation fills the week."""
    chosen = await make_bookable_mentor(db_engine, "pause-chosen")
    other = await make_bookable_mentor(db_engine, "pause-other")
    await api_client.put(
        f"{ADMIN}/{this_week()}", json={"mentor_id": str(chosen)}, headers=await an_admin(db_engine)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :u"),
            {"u": chosen},
        )

    public = (await api_client.get(PUBLIC)).json()

    assert public["id"] == str(other)


async def test_the_schedule_lists_weeks_with_who_chose_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    chosen = await make_bookable_mentor(db_engine, "list-chosen")
    headers = await an_admin(db_engine)
    next_week = this_week() + dt.timedelta(weeks=1)
    await api_client.put(f"{ADMIN}/{next_week}", json={"mentor_id": str(chosen)}, headers=headers)
    await api_client.get(PUBLIC)  # the rotation picks this week

    body = (await api_client.get(SCHEDULE, headers=headers)).json()

    by_week = {row["week_start"]: row for row in body["data"]}
    assert by_week[str(next_week)]["mentor_id"] == str(chosen)
    assert by_week[str(next_week)]["source"] == "admin"
    assert by_week[str(next_week)]["chosen_by"] is not None
    assert by_week[str(this_week())]["source"] == "automatic"
    assert by_week[str(this_week())]["chosen_by"] is None


async def test_the_schedule_is_for_admins_only(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    response = await api_client.get(SCHEDULE, headers=await an_admin(db_engine, None))

    # 404, not 403: the house rule for admin endpoints — a refusal would tell a
    # caller without a grant that the endpoint is real.
    assert response.status_code == 404


async def test_an_admin_row_must_name_its_admin(db_engine: AsyncEngine) -> None:
    """The constraint, asked directly: who chose a week is never unrecorded."""
    from sqlalchemy.exc import IntegrityError

    mentor = await make_bookable_mentor(db_engine, "ck-admin")
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO featured_mentors (mentor_user_id, week_start, cycle, source) "
                    "VALUES (:u, :w, 1, 'admin')"
                ),
                {"u": mentor, "w": this_week()},
            )

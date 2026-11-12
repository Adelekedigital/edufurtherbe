"""Weekly and monthly active users from this database (decision 342).

A signed-in request records its person's UTC day once; a preview records
nothing; the admin Overview counts people active in the 7 and 30 days ending
with the window's last day, internal accounts left out.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.test_api_admin_analytics_tabs import person, read
from tests.integration.test_api_preview_sessions import app_client
from tests.integration.test_api_preview_swap import live_preview, previewing

from app.api.deps import core
from app.domain.activity import SeenToday
from app.infra.db.demo_seed import DEMO_DOMAIN
from app.infra.storage.supabase import SupabaseStorage
from conftest import add_user, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ME = "/api/v1/me"


async def signed_in(engine: AsyncEngine, *, admin: bool = False) -> tuple[UUID, str]:
    auth = uuid4()
    async with engine.begin() as conn:
        user = await add_user(conn, f"{auth}@example.test", auth_id=auth, timezone="UTC")
        if admin:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user},
            )
    return user, api_token(auth)


async def days_of(engine: AsyncEngine, user: UUID) -> list[dt.date]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT day FROM user_activity_days WHERE user_id = :u ORDER BY day"), {"u": user}
        )
        return [row.day for row in rows]


async def active_on(engine: AsyncEngine, day: dt.date, *users: UUID) -> None:
    async with engine.begin() as conn:
        for user in users:
            await conn.execute(
                text("INSERT INTO user_activity_days (user_id, day) VALUES (:u, :d)"),
                {"u": user, "d": day},
            )


def utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


async def test_a_signed_in_request_records_its_utc_day_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, token = await signed_in(db_engine)
    before = utc_today()

    assert (await api_client.get(ME, headers=bearer(token))).status_code == 200
    assert (await api_client.get(ME, headers=bearer(token))).status_code == 200

    days = await days_of(db_engine, user)
    assert len(days) == 1
    assert days[0] in {before, utc_today()}


async def test_the_second_request_of_the_day_does_not_write(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user, token = await signed_in(db_engine)
    await api_client.get(ME, headers=bearer(token))
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM user_activity_days WHERE user_id = :u"), {"u": user})

    # Remembered: no write at all, so the deleted row stays gone.
    await api_client.get(ME, headers=bearer(token))
    assert await days_of(db_engine, user) == []

    # Forgotten (another process, a restart): the write goes again, and a
    # repeat of the same day is ignored rather than failing.
    monkeypatch.setattr(core, "SEEN_TODAY", SeenToday())
    await api_client.get(ME, headers=bearer(token))
    monkeypatch.setattr(core, "SEEN_TODAY", SeenToday())
    with caplog.at_level(logging.WARNING, logger=core.__name__):
        assert (await api_client.get(ME, headers=bearer(token))).status_code == 200
    assert len(await days_of(db_engine, user)) == 1
    assert "activity not recorded" not in caplog.messages


async def test_a_failed_recording_never_fails_the_request(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user, token = await signed_in(db_engine)

    async def broken(session: AsyncSession, *_: Any) -> None:
        await session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(core, "record_active_day", broken)
    with caplog.at_level(logging.WARNING, logger=core.__name__):
        answer = await api_client.get(ME, headers=bearer(token))

    assert answer.status_code == 200, answer.text
    assert answer.json()["id"] == str(user)
    assert "activity not recorded" in caplog.messages
    assert await days_of(db_engine, user) == []

    # Not remembered as done: the next request records it.
    monkeypatch.undo()
    await api_client.get(ME, headers=bearer(token))
    assert len(await days_of(db_engine, user)) == 1


@pytest.mark.isolated_db
async def test_a_preview_records_nobody(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with app_client(db_engine) as client:
        admin, user, preview = await live_preview(client, db_engine)
        # The admin's own requests count as the admin's, as anyone's do.
        assert len(await days_of(db_engine, admin.id)) == 1
        async with db_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM user_activity_days WHERE user_id = :u"), {"u": admin.id}
            )
        # Forget them both, so only the preview itself could write.
        monkeypatch.setattr(core, "SEEN_TODAY", SeenToday())

        as_target = await client.get(ME, headers=previewing(admin, preview))

    assert as_target.status_code == 200, as_target.text
    assert as_target.json()["id"] == str(user)
    assert await days_of(db_engine, user) == []
    assert await days_of(db_engine, admin.id) == []


async def test_the_overview_counts_7_and_30_days_ending_with_the_window(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    async with db_engine.begin() as conn:
        a, b, c, d, e, f = [await person(conn) for _ in range(6)]
        staff = await person(conn, admin=True)
    last = dt.date(2025, 3, 16)

    def on(days_before: int) -> dt.date:
        return last - dt.timedelta(days=days_before)

    await active_on(db_engine, on(0), a, staff)
    await active_on(db_engine, on(1), a)  # one person, two days: counted once
    await active_on(db_engine, on(6), b)  # the week's first day
    await active_on(db_engine, on(7), c, f)  # the day before it
    await active_on(db_engine, on(29), d)  # the month's first day
    await active_on(db_engine, on(30), e)  # the day before it

    window = {"from": "2025-03-16", "to": "2025-03-16", "tz": "UTC"}
    subs = (await read(db_engine, api_storage, "overview", **window))["sub_tracks"]
    # Now: week a, b; month a, b, c, f, d. The previous period is the 15th:
    # week (9th-15th) a, b, c, f; month (14 Feb-15th) all six.
    assert subs["weekly_active_users"] == {"value": 2, "previous": 4}
    assert subs["monthly_active_users"] == {"value": 5, "previous": 6}
    assert subs["stickiness"] == {"value": 40.0, "previous": 66.7}

    flat = (await read(db_engine, api_storage, "overview", **window, compare="false"))["sub_tracks"]
    assert flat["weekly_active_users"] == {"value": 2, "previous": None}
    assert flat["monthly_active_users"] == {"value": 5, "previous": None}
    assert flat["stickiness"] == {"value": 40.0, "previous": None}


async def test_an_internal_account_is_recorded_but_never_counted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    staff, token = await signed_in(db_engine, admin=True)
    member, member_token = await signed_in(db_engine)
    await api_client.get(ME, headers=bearer(token))
    await api_client.get(ME, headers=bearer(member_token))
    assert len(await days_of(db_engine, staff)) == 1
    assert len(await days_of(db_engine, member)) == 1
    today = utc_today().isoformat()

    subs = (await read(db_engine, api_storage, "overview", **{"from": today, "to": today}))[
        "sub_tracks"
    ]

    # The member alone: the staff account and the reading admin are internal.
    assert subs["weekly_active_users"]["value"] == 1
    assert subs["monthly_active_users"]["value"] == 1
    assert subs["stickiness"]["value"] == 100.0


async def test_the_window_ends_on_the_viewers_day(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    # Active at 22:00 on 16 March in New York: 02:00 UTC, so stored as the 17th.
    async with db_engine.begin() as conn:
        someone = await person(conn)
    await active_on(db_engine, dt.date(2025, 3, 17), someone)
    day = {"from": "2025-03-16", "to": "2025-03-16"}

    there = await read(db_engine, api_storage, "overview", **day, tz="America/New_York")
    utc = await read(db_engine, api_storage, "overview", **day, tz="UTC")

    assert there["sub_tracks"]["weekly_active_users"] == {"value": 1, "previous": 0}
    assert there["sub_tracks"]["stickiness"] == {"value": 100.0, "previous": None}
    assert utc["sub_tracks"]["weekly_active_users"]["value"] == 0
    assert utc["sub_tracks"]["stickiness"]["value"] is None


# --- Active members: by role and the weekly trend (decision 343) ------------


async def test_by_role_splits_the_totals_and_leaves_out_internal_accounts(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    async with db_engine.begin() as conn:
        ada, bo, di = await person(conn), await person(conn), await person(conn)
        cy = await add_user(conn, f"{uuid4()}@example.test", primary_role="mentor")
        staff = await person(conn, admin=True)
        demo = await add_user(conn, f"{uuid4()}@{DEMO_DOMAIN}", primary_role="mentor")
    last = dt.date(2025, 3, 16)
    await active_on(db_engine, last, ada, cy, staff, demo)
    await active_on(db_engine, last - dt.timedelta(days=1), bo, cy, staff, demo)
    await active_on(db_engine, last - dt.timedelta(days=10), di, staff, demo)  # month only

    day = {"from": "2025-03-16", "to": "2025-03-16"}
    body = await read(db_engine, api_storage, "overview", **day)
    subs, roles = body["sub_tracks"], body["active_members"]["by_role"]

    # Now: mentees week ada, bo; month ada, bo, di. Before (the 15th): week
    # bo; month bo, di. The live admin and the demo account count nowhere.
    assert roles["mentee"] == {
        "weekly": {"value": 2, "previous": 1},
        "monthly": {"value": 3, "previous": 2},
        "stickiness": {"value": 66.7, "previous": 50.0},
    }
    assert roles["mentor"] == {
        "weekly": {"value": 1, "previous": 1},
        "monthly": {"value": 1, "previous": 1},
        "stickiness": {"value": 100.0, "previous": 100.0},
    }
    for figure, total in (("weekly", "weekly_active_users"), ("monthly", "monthly_active_users")):
        for side in ("value", "previous"):
            split = roles["mentee"][figure][side] + roles["mentor"][figure][side]
            assert split == subs[total][side]
    assert "weekly_active_users" not in body["active_members"]


async def test_a_role_nobody_was_active_in_has_no_stickiness(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    async with db_engine.begin() as conn:
        ada = await person(conn)
    await active_on(db_engine, dt.date(2025, 3, 16), ada)

    day = {"from": "2025-03-16", "to": "2025-03-16"}
    roles = (await read(db_engine, api_storage, "overview", **day))["active_members"]["by_role"]

    assert roles["mentor"] == {
        "weekly": {"value": 0, "previous": 0},
        "monthly": {"value": 0, "previous": 0},
        "stickiness": {"value": None, "previous": None},
    }
    assert roles["mentee"]["stickiness"] == {"value": 100.0, "previous": None}


async def test_the_trend_is_7_day_actives_on_the_north_stars_points(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    async with db_engine.begin() as conn:
        x = await person(conn)
        y = await add_user(conn, f"{uuid4()}@example.test", primary_role="mentor")
        staff = await person(conn, admin=True)
    # Recording began on 20 Feb (partial): the first full day is the 21st.
    await active_on(db_engine, dt.date(2025, 2, 20), x)
    await active_on(db_engine, dt.date(2025, 2, 26), y, staff)
    await active_on(db_engine, dt.date(2025, 3, 3), x, staff)
    window = {"from": "2025-03-01", "to": "2025-03-14", "tz": "UTC"}

    body = await read(db_engine, api_storage, "overview", **window)
    series = body["active_members"]["series"]

    assert [p["start"] for p in series] == [p["start"] for p in body["north_star"]["series"]]
    # Each point is 7 days ending that day: y (26 Feb) reaches 4 March, x (3
    # March) the 9th. Before: 15-28 Feb, whose weeks start on the partial day
    # or earlier until the 27th (21-27 Feb).
    values = [1, 1, 2, 2, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    previous = [None] * 12 + [1, 1]
    assert [(p["value"], p["previous"]) for p in series] == list(zip(values, previous, strict=True))
    # x is a mentee, y a mentor; the live admin counts in neither.
    mentee = [0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    mentor = [1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert [p["by_role"]["mentee"]["value"] for p in series] == mentee
    assert [p["by_role"]["mentor"]["value"] for p in series] == mentor
    assert [p["by_role"]["mentee"]["previous"] for p in series] == [None] * 12 + [0, 0]
    assert [p["by_role"]["mentor"]["previous"] for p in series] == [None] * 12 + [1, 1]
    for p in series:
        for side in ("value", "previous"):
            if p[side] is not None:
                roles = p["by_role"]
                assert roles["mentee"][side] + roles["mentor"][side] == p[side]

    flat = (await read(db_engine, api_storage, "overview", **window, compare="false"))[
        "active_members"
    ]["series"]
    assert [(p["value"], p["previous"]) for p in flat] == [(v, None) for v in values]
    assert [p["by_role"]["mentee"] for p in flat] == [
        {"value": v, "previous": None} for v in mentee
    ]
    assert all(p["by_role"]["mentor"]["previous"] is None for p in flat)


async def test_weekly_points_before_recording_began_are_null(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    # Nothing recorded in 2025: the reading admin's own day today is the first.
    window = {"from": "2025-01-01", "to": "2025-03-31"}

    body = await read(db_engine, api_storage, "overview", **window)
    series = body["active_members"]["series"]

    assert body["north_star"]["unit"] == "week"
    assert [p["start"] for p in series] == [p["start"] for p in body["north_star"]["series"]]
    assert len(series) == 13
    assert all(p["value"] is None and p["previous"] is None for p in series)
    nothing = {"value": None, "previous": None}
    assert all(p["by_role"] == {"mentee": nothing, "mentor": nothing} for p in series)

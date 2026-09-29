"""A mentor's default length and notice, and offerings that follow them (#213).

Session Types frontend round 3 B, approved by the product owner 2026-09-29: the
"Use my defaults" mode. `default_duration_minutes` and
`default_min_notice_minutes` on the mentor profile; an offering whose own value
is null follows them, then the platform's (60 minutes, 24 hours) — the
`COALESCE(type, mentor, platform)` #204 uses for window and break.

**The 24-hour floor stands** (#104): a 6-hour option was asked for and declined
on 2026-09-29, because #121 expires an unanswered request six hours before the
session.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Column, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql import visitors
from tests.integration.factories import add_session_type
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, first_slot, key
from tests.integration.test_api_booking import body as booking
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body
from tests.integration.test_booking_window_break import NOW, at, mentor_with_hours

from app.infra.db.slot_store import list_slots
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def own(client: httpx.AsyncClient, auth: UUID, type_id: str) -> dict[str, object]:
    rows = (await client.get(URL, headers=bearer(api_token(auth)))).json()["data"]
    return next(row for row in rows if row["id"] == type_id)


async def mentor_defaults(
    engine: AsyncEngine, mentor: UUID, *, duration: int | None, notice: int | None
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET default_duration_minutes = :d, "
                "default_min_notice_minutes = :n WHERE user_id = :u"
            ),
            {"d": duration, "n": notice, "u": mentor},
        )


async def inheriting(engine: AsyncEngine, mentor: UUID, name: str) -> UUID:
    """An offering with neither length nor notice of its own."""
    session_type = await add_session_type(engine, mentor, name=name, duration=60, notice=0)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET duration_minutes = NULL, "
                "min_notice_minutes = NULL WHERE session_type_id = :t"
            ),
            {"t": session_type},
        )
    return session_type


async def starts(engine: AsyncEngine, mentor: UUID, session_type: UUID) -> list[dt.datetime]:
    async with AsyncSession(engine) as session:
        slots = await list_slots(
            session,
            mentor,
            session_type,
            start=NOW.date(),
            end=NOW.date() + dt.timedelta(days=7),
            now=NOW,
        )
    assert slots is not None
    return [slot.start for slot in slots]


# --------------------------------------------------------------------------
# The mentor's defaults
# --------------------------------------------------------------------------


async def test_a_mentor_sets_and_reads_their_length_and_notice(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "defaults-write")
    headers = bearer(api_token(auth))
    path = f"/api/v1/users/{mentor}/mentor-profile"

    written = await api_client.patch(
        path,
        json={"default_duration_minutes": 30, "default_min_notice_minutes": 2880},
        headers=headers,
    )
    profile = (await api_client.get(path, headers=headers)).json()

    assert written.status_code in (200, 204), written.text
    assert profile["default_duration_minutes"] == 30
    assert profile["default_min_notice_minutes"] == 2880


@pytest.mark.parametrize(
    "fields",
    [
        {"default_duration_minutes": 4},
        {"default_duration_minutes": 481},
        # The declined 6-hour option (#104, #121): the floor is 24 hours.
        {"default_min_notice_minutes": 360},
        {"default_min_notice_minutes": 4321},
    ],
)
async def test_defaults_outside_the_range_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fields: dict[str, int]
) -> None:
    mentor, auth = await as_mentor(db_engine, f"defaults-range-{next(iter(fields.values()))}")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile", json=fields, headers=bearer(api_token(auth))
    )

    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# An offering that inherits
# --------------------------------------------------------------------------


async def test_an_offering_created_without_length_or_notice_follows_the_defaults(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "inherit-create")
    await mentor_defaults(db_engine, mentor, duration=90, notice=4320)

    created = await api_client.post(
        URL,
        json={"name": "Follows", "duration_minutes": None},
        headers=bearer(api_token(auth)),
    )
    row = await own(api_client, auth, created.json()["id"])
    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert created.status_code == 201, created.text
    assert (row["duration_minutes"], row["min_notice_minutes"]) == (90, 4320)
    assert (row["duration_inherited"], row["min_notice_inherited"]) == (True, True)
    (listed,) = [t for t in public if t["id"] == created.json()["id"]]
    assert (listed["duration_minutes"], listed["min_notice_minutes"]) == (90, 4320)


async def test_with_no_default_anywhere_the_platform_applies(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "inherit-platform")

    created = await api_client.post(URL, json={"name": "Bare"}, headers=bearer(api_token(auth)))
    row = await own(api_client, auth, created.json()["id"])

    assert created.status_code == 201, created.text
    assert (row["duration_minutes"], row["min_notice_minutes"]) == (60, 1440)
    assert (row["duration_inherited"], row["min_notice_inherited"]) == (True, True)


async def test_an_offerings_own_values_win_and_patch_null_returns_to_inheriting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "inherit-patch")
    headers = bearer(api_token(auth))
    await mentor_defaults(db_engine, mentor, duration=30, notice=2880)
    created = (
        await api_client.post(
            URL, json=body(duration_minutes=45, min_notice_minutes=1440), headers=headers
        )
    ).json()

    own_values = await own(api_client, auth, created["id"])
    patched = await api_client.patch(
        f"{URL}/{created['id']}",
        json={"duration_minutes": None, "min_notice_minutes": None},
        headers=headers,
    )
    followed = await own(api_client, auth, created["id"])

    assert (own_values["duration_minutes"], own_values["min_notice_minutes"]) == (45, 1440)
    assert (own_values["duration_inherited"], own_values["min_notice_inherited"]) == (
        False,
        False,
    )
    assert patched.status_code == 200, patched.text
    assert (followed["duration_minutes"], followed["min_notice_minutes"]) == (30, 2880)
    assert (followed["duration_inherited"], followed["min_notice_inherited"]) == (True, True)


async def test_six_hours_notice_is_refused_on_an_offering_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The declined option, at the offering's boundary as well as the mentor's."""
    _, auth = await as_mentor(db_engine, "notice-six-hours")

    response = await api_client.post(
        URL, json=body(min_notice_minutes=360), headers=bearer(api_token(auth))
    )

    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# Slots and booking use the resolved values
# --------------------------------------------------------------------------


async def test_slots_step_by_the_inherited_length(db_engine: AsyncEngine) -> None:
    """The grid is stepped by duration, so a 30-minute default offers half-hours."""
    mentor = await mentor_with_hours(db_engine, "slots-inherit-length")
    await mentor_defaults(db_engine, mentor, duration=30, notice=1440)
    session_type = await inheriting(db_engine, mentor, "Half hours")

    found = await starts(db_engine, mentor, session_type)

    assert at(2, 9) in found
    assert at(2, 9) + dt.timedelta(minutes=30) in found


async def test_slots_respect_the_inherited_notice(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "slots-inherit-notice")
    await mentor_defaults(db_engine, mentor, duration=60, notice=4320)
    session_type = await inheriting(db_engine, mentor, "Three days out")

    found = await starts(db_engine, mentor, session_type)

    assert found
    assert min(found) >= NOW + dt.timedelta(minutes=4320)
    assert at(3, 9) in found


async def test_with_no_default_slots_use_the_platform_length_and_floor(
    db_engine: AsyncEngine,
) -> None:
    mentor = await mentor_with_hours(db_engine, "slots-platform")
    session_type = await inheriting(db_engine, mentor, "Platform")

    found = await starts(db_engine, mentor, session_type)

    assert min(found) >= NOW + dt.timedelta(minutes=1440)
    assert at(2, 9) + dt.timedelta(minutes=60) in found
    assert at(2, 9) + dt.timedelta(minutes=30) not in found


async def test_a_booking_takes_the_inherited_length(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The session row records the length the slot was offered at — resolved
    the way `/slots` resolves it, so the two cannot disagree."""
    mentor, session_type = await a_bookable_offering(db_engine, "book-inherit")
    await mentor_defaults(db_engine, mentor, duration=30, notice=None)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET duration_minutes = NULL "
                "WHERE session_type_id = :t"
            ),
            {"t": session_type},
        )
    _, headers = await a_mentee(db_engine, "book-inherit")

    response = await api_client.post(
        "/api/v1/sessions",
        json=booking(session_type, await first_slot(api_client, mentor, session_type)),
        headers=headers | key(),
    )

    assert response.status_code == 201, response.text
    async with db_engine.begin() as conn:
        stored = (
            await conn.execute(
                text("SELECT duration_minutes FROM sessions WHERE mentor_id = :m"), {"m": mentor}
            )
        ).scalar_one()
    assert stored == 30


# --------------------------------------------------------------------------
# The next-free-time cache hears about a default changing
# --------------------------------------------------------------------------


async def test_the_availability_trigger_watches_every_mentor_column_slots_read(
    db_engine: AsyncEngine,
) -> None:
    """`trg_log_availability_change` on `mentor_profiles` fires only for named
    columns, so a mentor column the slot rules start reading must be named too —
    or a mentor who changes a default keeps a stale next free time on every
    card. #204 began reading two without adding them; this fails the day the
    rules and the trigger diverge again."""
    from app.infra.db.booking_rules import (
        effective_break_minutes,
        effective_duration_minutes,
        effective_min_notice_minutes,
        effective_window_days,
    )

    read = {
        element.name
        for rule in (
            effective_duration_minutes(),
            effective_min_notice_minutes(),
            effective_window_days(),
            effective_break_minutes(),
        )
        for element in visitors.iterate(rule)
        if isinstance(element, Column) and element.table.name == "mentor_profiles"
    }
    assert read >= {"default_duration_minutes", "booking_window_days"}

    async with db_engine.begin() as conn:
        watched = set(
            (
                await conn.execute(
                    text(
                        "SELECT a.attname FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "CROSS JOIN LATERAL unnest(t.tgattr::int2[]) AS k(attnum) "
                        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
                        "WHERE t.tgname = 'trg_log_availability_change' "
                        "AND c.relname = 'mentor_profiles'"
                    )
                )
            ).scalars()
        )

    assert read <= watched, read - watched


async def test_changing_a_default_logs_an_availability_change(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "defaults-logged")

    async def logged() -> int:
        async with db_engine.begin() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM mentor_availability_changes "
                            "WHERE mentor_user_id = :m"
                        ),
                        {"m": mentor},
                    )
                ).scalar_one()
            )

    before = await logged()
    await mentor_defaults(db_engine, mentor, duration=30, notice=None)

    assert await logged() == before + 1

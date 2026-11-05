"""`GET /mentor-schools`: where the mentors you can book studied (decision 307)."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_education,
    add_session_type,
    make_bookable_mentor,
    make_institution,
    make_public_mentor,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentor-schools"


async def mentor_at(engine: AsyncEngine, tag: str, institution: UUID) -> UUID:
    mentor = await make_bookable_mentor(engine, tag)
    await add_education(engine, mentor, institution_id=institution)
    return mentor


async def names(client: httpx.AsyncClient, query: str = "") -> list[str]:
    response = await client.get(f"{URL}{query}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    return [row["name"] for row in body["data"]]


async def test_most_mentors_first_then_by_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    oxford = await make_institution(db_engine, "University of Oxford")
    lagos = await make_institution(db_engine, "University of Lagos")
    ashesi = await make_institution(db_engine, "Ashesi University")
    for i in range(3):
        await mentor_at(db_engine, f"ox{i}", oxford)
    await mentor_at(db_engine, "lag", lagos)
    await mentor_at(db_engine, "ash", ashesi)

    response = await api_client.get(URL)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=600"
    assert response.json() == {
        "data": [
            {"name": "University of Oxford"},
            {"name": "Ashesi University"},
            {"name": "University of Lagos"},
        ],
        "next_cursor": None,
    }


async def test_nobody_bookable_is_an_empty_page(api_client: httpx.AsyncClient) -> None:
    assert await names(api_client) == []


@pytest.mark.parametrize(("limit", "ok"), [(0, False), (1, True), (20, True), (21, False)])
async def test_limit_bounds(api_client: httpx.AsyncClient, limit: int, ok: bool) -> None:
    response = await api_client.get(f"{URL}?limit={limit}")
    assert (response.status_code == 200) is ok, response.text


async def test_limit_defaults_to_five_and_cuts_the_tail(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    for i in range(7):
        await mentor_at(db_engine, f"s{i}", await make_institution(db_engine, f"School {i}"))

    assert await names(api_client) == [f"School {i}" for i in range(5)]
    assert await names(api_client, "?limit=2") == ["School 0", "School 1"]


async def test_only_mentors_taking_bookings_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Pending, unlisted, not set up, or deleted: none is counted."""
    oxford = await make_institution(db_engine, "University of Oxford")
    await mentor_at(db_engine, "lag", await make_institution(db_engine, "University of Lagos"))

    for tag, knobs in [("pend", {"approved": False}), ("unl", {"listed": False})]:
        hidden = await make_public_mentor(db_engine, tag, **knobs)
        await add_session_type(db_engine, hidden)
        await add_education(db_engine, hidden, institution_id=oxford)
    idle = await make_public_mentor(db_engine, "idle")  # no offering, no hours
    await add_education(db_engine, idle, institution_id=oxford)
    gone = await mentor_at(db_engine, "gone", oxford)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": gone})

    assert await names(api_client) == ["University of Lagos"]


async def test_typed_pending_and_merged_schools_are_left_out(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor whose card shows a typed, uncurated or merged-away school is not
    counted, not even under another entry of theirs; a catalogue school is."""
    oxford = await make_institution(db_engine, "University of Oxford")
    pending = await make_institution(db_engine, "Pending Polytechnic", status="pending_review")
    merged = await make_institution(db_engine, "Merged College", status="merged")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE institutions SET merged_into_id = :o WHERE id = :m"),
            {"o": oxford, "m": merged},
        )

    typed = await make_bookable_mentor(db_engine, "typed")
    await add_education(db_engine, typed, school="Somewhere Unlisted", institution=False)
    await add_education(
        db_engine, typed, level="bachelors", institution_id=oxford, date_end="2010-01-01"
    )
    await mentor_at(db_engine, "pend", pending)
    await mentor_at(db_engine, "merg", merged)
    await mentor_at(db_engine, "lag", await make_institution(db_engine, "University of Lagos"))

    assert await names(api_client) == ["University of Lagos"]


async def test_a_school_is_the_one_the_card_shows(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Highest qualification first, so a later bachelor's does not move the
    mentor from the school of their doctorate."""
    oxford = await make_institution(db_engine, "University of Oxford")
    lagos = await make_institution(db_engine, "University of Lagos")
    mentor = await make_bookable_mentor(db_engine, "both")
    await add_education(db_engine, mentor, level="doctorate", institution_id=oxford)
    await add_education(
        db_engine, mentor, level="bachelors", institution_id=lagos, date_end="2030-01-01"
    )

    assert await names(api_client) == ["University of Oxford"]


async def test_one_count_per_mentor(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    """Two degrees at one school count the mentor once: Lagos (two mentors)
    stays ahead of Oxford (one mentor, two degrees there)."""
    oxford = await make_institution(db_engine, "University of Oxford")
    lagos = await make_institution(db_engine, "University of Lagos")
    twice = await mentor_at(db_engine, "twice", oxford)
    await add_education(db_engine, twice, institution_id=oxford, date_end="2010-01-01")
    await mentor_at(db_engine, "l1", lagos)
    await mentor_at(db_engine, "l2", lagos)

    assert await names(api_client) == ["University of Lagos", "University of Oxford"]


async def test_schools_sharing_a_name_are_one_entry(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two catalogue rows called City University (different countries) are one
    name on the strip, counted together: 2 puts it ahead of Aston's 1, where
    either row alone would tie and sort after Aston by name."""
    await mentor_at(db_engine, "c1", await make_institution(db_engine, "City University"))
    await mentor_at(db_engine, "c2", await make_institution(db_engine, "City University"))
    await mentor_at(db_engine, "zz", await make_institution(db_engine, "Aston University"))

    assert await names(api_client) == ["City University", "Aston University"]


async def test_a_signed_in_request_reads_the_shared_answer(
    api_client: httpx.AsyncClient,
) -> None:
    """Outside the per-viewer `/mentors` root, so a token changes nothing."""
    response = await api_client.get(URL, headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=600"
    assert "authorization" not in response.headers.get("vary", "").lower()


async def test_a_school_with_no_name_is_left_out(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only catalogue schools with a display name: a blank one would render as
    an empty chip."""
    await mentor_at(db_engine, "blank", await make_institution(db_engine, "   "))
    await mentor_at(db_engine, "lag", await make_institution(db_engine, "University of Lagos"))

    assert await names(api_client) == ["University of Lagos"]

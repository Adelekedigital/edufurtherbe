"""A profile is public only while its mentor can be booked (settled decision #192).

Frontend #15, product-approved, widened by the owner to one rule: an approved,
listed mentor with no offering or no weekly hours is left out of Explore — as
before — **and** their profile and reviews are a 404 to everyone but them. The
owner still reads it, with `setup_needed` naming what is missing, and going
live is automatic the moment both exist.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_session_type,
    make_bookable_mentor,
    make_public_mentor,
)
from tests.integration.test_mentor_owner_view import a_stranger, auth_of

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def url(handle: object, tail: str = "") -> str:
    return f"/api/v1/mentors/{handle}{tail}"


async def test_a_mentor_with_no_hours_is_hidden_from_strangers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "no-hours")
    await add_session_type(db_engine, mentor)

    anonymous = await api_client.get(url(mentor))
    signed_in = await api_client.get(url(mentor), headers=await a_stranger(db_engine, "no-hours"))

    assert anonymous.status_code == signed_in.status_code == 404


async def test_a_mentor_with_no_offering_is_hidden_from_strangers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "no-offering")
    await add_availability(db_engine, mentor)

    assert (await api_client.get(url(mentor))).status_code == 404


async def test_a_bookable_mentor_is_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "live")

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert "setup_needed" not in response.json()


async def test_setting_hours_puts_the_profile_live_at_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "goes-live")
    await add_session_type(db_engine, mentor)
    before = await api_client.get(url(mentor))

    await add_availability(db_engine, mentor)
    after = await api_client.get(url(mentor))

    assert before.status_code == 404
    assert after.status_code == 200


@pytest.mark.parametrize(
    ("offering", "hours", "needed"),
    [
        (True, False, ["weekly_hours"]),
        (False, True, ["session_type"]),
        (False, False, ["session_type", "weekly_hours"]),
        (True, True, []),
    ],
)
async def test_the_owner_is_told_what_is_missing(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    offering: bool,
    hours: bool,
    needed: list[str],
) -> None:
    mentor = await make_public_mentor(db_engine, f"setup-{offering}-{hours}".lower())
    if offering:
        await add_session_type(db_engine, mentor)
    if hours:
        await add_availability(db_engine, mentor)

    response = await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))

    assert response.status_code == 200
    assert response.json()["setup_needed"] == needed


async def test_the_reviews_of_a_hidden_mentor_are_hidden_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "no-hours-reviews")
    await add_session_type(db_engine, mentor)

    anonymous = await api_client.get(url(mentor, "/reviews"))
    owner = await api_client.get(url(mentor, "/reviews"), headers=await auth_of(db_engine, mentor))

    assert anonymous.status_code == 404
    assert owner.status_code == 200


async def test_a_hidden_mentor_has_no_similar_page(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "no-hours-similar")
    await add_session_type(db_engine, mentor)

    assert (await api_client.get(url(mentor, "/similar"))).status_code == 404

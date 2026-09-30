"""A mentor sees how complete their profile is, and what to do next (#223).

Frontend request (Profile strength card): the owner's own profile read carries
`completeness: {percent, missing}`, where `missing` is ordered by what the mentor
should do next — the two that stop anyone booking them first. Nobody else ever
receives it, exactly as with `setup_needed`.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_education,
    add_session_type,
    make_public_mentor,
)
from tests.integration.test_api_mentor_profile_sections import add_award, add_language
from tests.integration.test_mentor_owner_view import a_stranger, auth_of, url

from app.domain.profile_strength import COMPLETENESS_ORDER

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def bare_mentor(engine: AsyncEngine, tag: str) -> object:
    """A mentor with nothing filled in — the factory's placeholder headline cleared."""
    mentor = await make_public_mentor(engine, tag)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET headline = NULL WHERE user_id = :u"), {"u": mentor}
        )
    return mentor


async def fill_profile(engine: AsyncEngine, mentor: object) -> None:
    """Every item except session type, hours, education, award and languages."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET headline = 'I help with PhD applications', "
                "primary_study_country_id = (SELECT id FROM countries WHERE code = 'GB') "
                "WHERE user_id = :u"
            ),
            {"u": mentor},
        )
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, about_me, avatar_url, origin_country_id) "
                "VALUES (:u, 'Ten years in admissions.', 'https://img.example/a.jpg', "
                "(SELECT id FROM countries WHERE code = 'NG'))"
            ),
            {"u": mentor},
        )
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings ORDER BY slug LIMIT 1"
            ),
            {"u": mentor},
        )


async def owner_view(client: httpx.AsyncClient, engine: AsyncEngine, mentor: object) -> dict:
    response = await client.get(url(mentor), headers=await auth_of(engine, mentor))
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_bare_mentor_is_at_zero_with_every_step_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await bare_mentor(db_engine, "strength-bare")

    body = await owner_view(api_client, db_engine, mentor)

    assert body["completeness"] == {"percent": 0, "missing": list(COMPLETENESS_ORDER)}
    assert body["setup_needed"] == ["session_type", "weekly_hours"]


async def test_a_full_profile_is_at_a_hundred_with_nothing_missing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await bare_mentor(db_engine, "strength-full")
    await fill_profile(db_engine, mentor)
    await add_session_type(db_engine, mentor)
    await add_availability(db_engine, mentor)
    await add_education(db_engine, mentor)
    await add_award(db_engine, mentor)
    await add_language(db_engine, mentor, "French")

    body = await owner_view(api_client, db_engine, mentor)

    assert body["completeness"] == {"percent": 100, "missing": []}
    assert body["setup_needed"] == []


async def test_a_partial_profile_counts_what_is_done_and_orders_the_rest(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Bookable, with a photo, headline, about and topics — but the background
    lacks a language, so it is not done, and neither are education or award.
    A deleted award does not count."""
    mentor = await bare_mentor(db_engine, "strength-partial")
    await fill_profile(db_engine, mentor)
    await add_session_type(db_engine, mentor)
    await add_availability(db_engine, mentor)
    await add_award(db_engine, mentor, deleted=True)
    await add_education(db_engine, mentor, deleted=True)

    body = await owner_view(api_client, db_engine, mentor)

    assert body["completeness"] == {
        "percent": 67,
        "missing": ["background", "education", "award"],
    }
    assert body["setup_needed"] == []


async def test_nobody_but_the_owner_receives_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await bare_mentor(db_engine, "strength-private")
    await add_session_type(db_engine, mentor)
    await add_availability(db_engine, mentor)

    anonymous = await api_client.get(url(mentor))
    stranger = await api_client.get(
        url(mentor), headers=await a_stranger(db_engine, "strength-private")
    )

    assert anonymous.status_code == 200, anonymous.text
    assert stranger.status_code == 200, stranger.text
    assert "completeness" not in anonymous.json()
    assert "completeness" not in stranger.json()

"""A mentor application with every item filled, so a test removes exactly one.

Beside `factories.py` rather than in it: that module is near the size limit
(rule 11), and only the application suites need this.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type, make_public_mentor


async def complete_application(engine: AsyncEngine, mentor: UUID) -> None:
    """Fill every item `missing_items` checks, on a mentor who already has a profile."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles "
                "(user_id, avatar_url, origin_country_id, social_linkedin) "
                "VALUES (:u, 'https://cdn.example.test/a.png', "
                "        (SELECT id FROM countries WHERE code = 'NG'), "
                "        'https://www.linkedin.com/in/ada') "
                "ON CONFLICT (user_id) DO UPDATE SET avatar_url = EXCLUDED.avatar_url, "
                "origin_country_id = EXCLUDED.origin_country_id, "
                "social_linkedin = EXCLUDED.social_linkedin"
            ),
            {"u": mentor},
        )
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET headline = 'MSc Data Science · Oxford', "
                "primary_study_country_id = (SELECT id FROM countries WHERE code = 'GB') "
                "WHERE user_id = :u"
            ),
            {"u": mentor},
        )
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings WHERE slug = 'test-preparation'"
            ),
            {"u": mentor},
        )
        await conn.execute(
            text(
                "INSERT INTO education_entries "
                "(user_id, school_name_raw, degree_level_id, study_course, date_start, date_end) "
                "SELECT :u, 'Oxford', id, 'Data Science', :start, :end "
                "FROM degree_levels WHERE slug = 'masters'"
            ),
            {"u": mentor, "start": dt.date(2020, 1, 1), "end": dt.date(2021, 12, 31)},
        )
    await add_session_type(engine, mentor, name="Intro chat", duration=30)
    await add_availability(engine, mentor)


async def make_applicant(engine: AsyncEngine, tag: str, *, complete: bool = True) -> UUID:
    """A pending, unlisted mentor, as `POST /mentor-profile` leaves one, filled in
    unless `complete=False`."""
    mentor = await make_public_mentor(engine, tag, approved=False, listed=False)
    if complete:
        await complete_application(engine, mentor)
    return mentor

"""A submitted application's reviewed fields hold still until the decision
(owner, 2026-10-18).

Refused with 409 `/problems/mentor-application-in-review` and `fields`; only a
change counts, and approve or decline unlocks.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.application_factories import make_applicant, run_sql
from tests.integration.factories import token_for, until_blocked

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

TYPE = "/problems/mentor-application-in-review"


async def submitted(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    """A complete application, submitted and pending."""
    mentor = await make_applicant(engine, tag)
    await run_sql(
        engine, "UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u", {"u": mentor}
    )
    return mentor, bearer(await token_for(engine, mentor))


async def an_entry(engine: AsyncEngine, mentor: UUID) -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text("SELECT id FROM education_entries WHERE user_id = :u"), {"u": mentor}
                    )
                ).scalar_one()
            )
        )


def refused(response: httpx.Response, fields: list[str]) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["type"] == TYPE
    assert response.json()["fields"] == fields


# --------------------------------------------------------------------------
# Education: every write changes it
# --------------------------------------------------------------------------


async def test_education_cannot_be_added_changed_or_removed_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-edu")
    entry = await an_entry(db_engine, mentor)
    base = f"/api/v1/users/{mentor}/education"

    added = await api_client.post(base, headers=auth, json={"school_name_raw": "Lagos"})
    changed = await api_client.patch(f"{base}/{entry}", headers=auth, json={"study_course": "Law"})
    removed = await api_client.delete(f"{base}/{entry}", headers=auth)

    for response in (added, changed, removed):
        refused(response, ["education"])
    async with db_engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT count(*), max(study_course) FROM education_entries WHERE user_id = :u"),
            {"u": mentor},
        )
        assert tuple(rows.one()) == (1, "Data Science")


@pytest.mark.parametrize("state", ["unsubmitted", "approved", "declined"])
async def test_education_is_writable_before_submitting_and_after_the_decision(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, state: str
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-open-{state}")
    sql = {
        "unsubmitted": "UPDATE mentor_profiles SET submitted_at = NULL WHERE user_id = :u",
        "approved": "UPDATE mentor_profiles SET approval_status = 'approved' WHERE user_id = :u",
        "declined": "UPDATE mentor_profiles SET approval_status = 'declined' WHERE user_id = :u",
    }[state]
    await run_sql(db_engine, sql, {"u": mentor})

    response = await api_client.post(
        f"/api/v1/users/{mentor}/education", headers=auth, json={"school_name_raw": "Lagos"}
    )

    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# Profile and mentor profile: only a change is refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"social_linkedin": "https://www.linkedin.com/in/someone-else"}, ["social_linkedin"]),
        ({"about_me": "A new bio"}, ["about_me"]),
        (
            {"about_me": "A new bio", "social_linkedin": "linkedin.com/in/someone-else"},
            ["social_linkedin", "about_me"],
        ),
    ],
    ids=["linkedin", "about", "both"],
)
async def test_a_profile_write_changing_a_reviewed_field_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, str], fields: list[str]
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-prof-{fields[-1]}-{len(fields)}")

    response = await api_client.patch(f"/api/v1/users/{mentor}/profile", headers=auth, json=body)

    refused(response, fields)


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Europe/London"},
        {"primary_role": "mentee"},
        # The stored link resent in another spelling is not a change.
        {"social_linkedin": "linkedin.com/in/ada", "timezone": "Europe/London"},
    ],
    ids=["timezone", "primary-role", "same-linkedin"],
)
async def test_a_profile_write_that_leaves_reviewed_fields_alone_saves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, str]
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-ok-{sorted(body)[0]}-{len(body)}")

    response = await api_client.patch(f"/api/v1/users/{mentor}/profile", headers=auth, json=body)

    assert response.status_code == 204, response.text


async def test_a_mentor_profile_write_changing_a_reviewed_field_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-mp")
    url = f"/api/v1/users/{mentor}/mentor-profile"
    other = await _offering(db_engine, "document-preparation")

    headline = await api_client.patch(url, headers=auth, json={"headline": "New"})
    offerings = await api_client.patch(url, headers=auth, json={"offering_ids": [str(other)]})
    country = await api_client.patch(
        url, headers=auth, json={"primary_study_country_id": str(await _country(db_engine, "US"))}
    )

    refused(headline, ["headline"])
    refused(offerings, ["offering_ids"])
    refused(country, ["primary_study_country_id"])


async def test_a_mentor_profile_write_that_leaves_reviewed_fields_alone_saves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-mp-ok")
    same = await _offering(db_engine, "test-preparation")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={
            "requires_booking_confirmation": True,
            "headline": "MSc Data Science · Oxford",
            "offering_ids": [str(same)],
        },
    )

    assert response.status_code == 200, response.text


async def _offering(engine: AsyncEngine, slug: str) -> UUID:
    async with engine.begin() as conn:
        found = await conn.execute(
            text("SELECT id FROM service_offerings WHERE slug = :s"), {"s": slug}
        )
        return UUID(str(found.scalar_one()))


async def _country(engine: AsyncEngine, code: str) -> UUID:
    async with engine.begin() as conn:
        found = await conn.execute(text("SELECT id FROM countries WHERE code = :c"), {"c": code})
        return UUID(str(found.scalar_one()))


async def test_a_legacy_stored_link_is_compared_in_its_canonical_form(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A link the old system stored in another spelling is still the same link."""
    mentor, auth = await submitted(db_engine, "frz-legacy")
    await run_sql(
        db_engine,
        "UPDATE user_profiles SET social_linkedin = 'linkedin.com/in/ada/' WHERE user_id = :u",
        {"u": mentor},
    )

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/profile",
        headers=auth,
        json={"social_linkedin": "https://www.linkedin.com/in/ada"},
    )

    assert response.status_code == 204, response.text


async def test_a_programme_change_and_a_redraft_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-prog")

    programme = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={"primary_study_program": "Law"},
    )
    redraft = await api_client.patch(
        f"/api/v1/users/{mentor}/profile",
        headers=auth,
        json={
            "about_me": "A drafted bio",
            "about_me_drafted": True,
            "about_me_prompt_version": "bio_draft-v1",
        },
    )

    refused(programme, ["primary_study_program"])
    refused(redraft, ["about_me"])


async def test_stored_text_is_compared_the_way_a_write_cleans_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A legacy headline with trailing space, resent trimmed, is not a change."""
    mentor, auth = await submitted(db_engine, "frz-space")
    await run_sql(
        db_engine,
        "UPDATE mentor_profiles SET headline = 'MSc Data Science · Oxford  ' WHERE user_id = :u",
        {"u": mentor},
    )

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={"headline": "MSc Data Science · Oxford", "requires_booking_confirmation": True},
    )

    assert response.status_code == 200, response.text


async def test_clearing_a_stored_link_that_is_not_canonical_is_still_a_change(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A legacy value the rule cannot read is kept as itself, so it cannot be
    cleared out from under the reviewer."""
    mentor, auth = await submitted(db_engine, "frz-oddlink")
    await run_sql(
        db_engine,
        "UPDATE user_profiles SET social_linkedin = 'https://linkedin.test/ada' WHERE user_id = :u",
        {"u": mentor},
    )

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/profile", headers=auth, json={"social_linkedin": None}
    )

    refused(response, ["social_linkedin"])


async def test_an_edit_racing_a_submit_waits_for_it_and_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The race the lock order closes.** A submit holds the mentor row; an edit
    started meanwhile waits on it, then sees the application submitted and is
    refused, instead of reading "not yet submitted" and landing after."""
    mentor, auth = await submitted(db_engine, "frz-race")
    await run_sql(
        db_engine,
        "UPDATE mentor_profiles SET submitted_at = NULL WHERE user_id = :u",
        {"u": mentor},
    )
    async with db_engine.connect() as submit:
        transaction = await submit.begin()
        await submit.execute(
            text("SELECT 1 FROM mentor_profiles WHERE user_id = :u FOR UPDATE"), {"u": mentor}
        )
        edit = asyncio.create_task(
            api_client.patch(
                f"/api/v1/users/{mentor}/mentor-profile", headers=auth, json={"headline": "New"}
            )
        )
        await until_blocked(db_engine)
        await submit.execute(
            text("UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )
        await transaction.commit()

    refused(await edit, ["headline"])

"""Where a bio came from (Onboarding v5 #8, decisions 266 and 267).

`PATCH /users/{id}/profile` takes `about_me_drafted` and `about_me_prompt_version`;
the server stamps the time on its own clock. The owner reads it back on `GET /me`,
and a mentor on their own public profile; nobody else ever sees it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor, token_for
from tests.integration.test_api_profile_owner_edits import a_user, pointers
from tests.integration.test_api_writes import url
from tests.integration.test_mentor_owner_view import a_stranger

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DRAFTED = {
    "about_me": "A drafted bio.",
    "about_me_drafted": True,
    "about_me_prompt_version": "bio_draft-v1",
}


async def stored(engine: AsyncEngine, user_id: UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT about_me, about_me_drafted_at, about_me_prompt_version, "
                        "abs(extract(epoch FROM now() - about_me_drafted_at)) < 60 AS just_now "
                        "FROM user_profiles WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def patch(
    client: httpx.AsyncClient, user_id: UUID, headers: dict[str, str], **body: object
) -> httpx.Response:
    return await client.patch(url(user_id, "profile"), json=body, headers=headers)


async def test_saving_a_suggestion_stamps_it_and_me_reads_it_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "drafted")

    response = await patch(api_client, user_id, headers, **DRAFTED)

    assert response.status_code == 204
    row = await stored(db_engine, user_id)
    assert row["about_me_prompt_version"] == "bio_draft-v1"
    assert row["just_now"] is True
    me = (await api_client.get("/api/v1/me", headers=headers)).json()
    assert me["profile"]["about_me_drafted_at"] is not None
    assert "about_me_prompt_version" not in me["profile"]


async def test_a_client_timestamp_is_never_used(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "client-clock")

    await patch(api_client, user_id, headers, **DRAFTED, about_me_drafted_at="2001-01-01T00:00:00Z")

    assert (await stored(db_engine, user_id))["just_now"] is True


async def test_saying_it_is_your_own_clears_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "own")
    await patch(api_client, user_id, headers, **DRAFTED)

    response = await patch(api_client, user_id, headers, about_me_drafted=False)

    assert response.status_code == 204
    row = await stored(db_engine, user_id)
    assert (row["about_me"], row["about_me_drafted_at"], row["about_me_prompt_version"]) == (
        "A drafted bio.",
        None,
        None,
    )
    me = (await api_client.get("/api/v1/me", headers=headers)).json()
    assert me["profile"]["about_me_drafted_at"] is None


async def test_the_record_follows_the_text(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A form resending the same bio keeps the record; other text clears it, and
    so does clearing the bio."""
    user_id, headers = await a_user(db_engine, "follows")
    await patch(api_client, user_id, headers, **DRAFTED)

    await patch(api_client, user_id, headers, about_me="A drafted bio.", gender="female")
    assert (await stored(db_engine, user_id))["about_me_prompt_version"] == "bio_draft-v1"

    await patch(api_client, user_id, headers, gender="male")
    assert (await stored(db_engine, user_id))["about_me_prompt_version"] == "bio_draft-v1"

    await patch(api_client, user_id, headers, about_me="My own words now.")
    row = await stored(db_engine, user_id)
    assert (row["about_me_drafted_at"], row["about_me_prompt_version"]) == (None, None)

    await patch(api_client, user_id, headers, **DRAFTED)
    response = await patch(api_client, user_id, headers, about_me=None)
    assert response.status_code == 204
    row = await stored(db_engine, user_id)
    assert (row["about_me"], row["about_me_drafted_at"]) == (None, None)


async def test_a_first_save_can_be_a_suggestion(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No profile row yet: the insert carries the stamp."""
    user_id, headers = await a_user(db_engine, "first")

    await patch(
        api_client, user_id, headers, **{**DRAFTED, "about_me_prompt_version": "bio_improve-v2"}
    )

    assert (await stored(db_engine, user_id))["about_me_prompt_version"] == "bio_improve-v2"


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ({"about_me_prompt_version": "bio_draft-v1"}, "/about_me_prompt_version"),
        ({**DRAFTED, "about_me_drafted": False}, "/about_me_prompt_version"),
        ({"about_me": "x", "about_me_drafted": True}, "/about_me_prompt_version"),
        (
            {**DRAFTED, "about_me_prompt_version": "scholarship_extraction-v1"},
            "/about_me_prompt_version",
        ),
        ({**DRAFTED, "about_me_prompt_version": "Bio_draft-v1"}, "/about_me_prompt_version"),
        (
            {**DRAFTED, "about_me_prompt_version": "bio_draft-v" + "1" * 54},
            "/about_me_prompt_version",
        ),
        ({**DRAFTED, "about_me_prompt_version": None}, "/about_me_prompt_version"),
        ({"about_me_drafted": True, "about_me_prompt_version": "bio_draft-v1"}, "/about_me"),
        ({**DRAFTED, "about_me": "   "}, "/about_me"),
        ({"about_me_drafted": None}, "/about_me_drafted"),
    ],
    ids=[
        "version-alone",
        "version-with-false",
        "no-version",
        "not-a-bio-task",
        "malformed",
        "sixty-five",
        "null-version",
        "no-text",
        "blank-text",
        "null-flag",
    ],
)
async def test_a_malformed_provenance_is_a_422_and_writes_nothing(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    body: dict[str, object],
    pointer: str,
) -> None:
    user_id, headers = await a_user(db_engine, "bad")
    await patch(api_client, user_id, headers, about_me="Before.")

    response = await patch(api_client, user_id, headers, **body, gender="female")

    assert response.status_code == 422
    assert pointer in pointers(response)
    row = await stored(db_engine, user_id)
    assert (row["about_me"], row["about_me_drafted_at"]) == ("Before.", None)


async def test_a_mentor_sees_it_on_their_own_profile_and_nobody_else_does(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "drafted-mentor", slug="drafted-mentor")
    owner = {"Authorization": f"Bearer {await token_for(db_engine, mentor)}"}
    await patch(api_client, mentor, owner, **DRAFTED)

    mine = (await api_client.get(f"/api/v1/mentors/{mentor}", headers=owner)).json()
    theirs = await api_client.get(
        f"/api/v1/mentors/{mentor}", headers=await a_stranger(db_engine, "drafted")
    )
    anonymous = await api_client.get(f"/api/v1/mentors/{mentor}")

    assert mine["about_me_drafted_at"] is not None
    assert theirs.status_code == anonymous.status_code == 200
    assert "about_me_drafted_at" not in theirs.json()
    assert "about_me_drafted_at" not in anonymous.json()
    assert theirs.json()["about_me"] == "A drafted bio."


async def test_an_own_bio_is_absent_from_the_mentors_own_view(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "own-mentor", slug="own-mentor")
    owner = {"Authorization": f"Bearer {await token_for(db_engine, mentor)}"}

    mine = (await api_client.get(f"/api/v1/mentors/{mentor}", headers=owner)).json()

    assert "approval_status" in mine
    assert "about_me_drafted_at" not in mine


async def test_saying_it_is_your_own_creates_no_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Nothing to clear, so no row: `/me`'s `profile` stays null."""
    user_id, headers = await a_user(db_engine, "own-no-profile")

    response = await patch(api_client, user_id, headers, about_me_drafted=False)

    assert response.status_code == 204
    me = (await api_client.get("/api/v1/me", headers=headers)).json()
    assert me["profile"] is None


async def test_a_later_prompt_version_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Pattern-based: the router's v2, and any bump after it, save."""
    user_id, headers = await a_user(db_engine, "v2")

    for version in ("bio_draft-v2", "bio_improve-v12"):
        response = await patch(
            api_client, user_id, headers, **{**DRAFTED, "about_me_prompt_version": version}
        )
        assert response.status_code == 204
        assert (await stored(db_engine, user_id))["about_me_prompt_version"] == version

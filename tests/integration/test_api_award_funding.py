"""Whether an award was fully or partly funded, as the mentor says (#189).

Nothing in the data carried this: no award field, and the programme's own
`funding_type` is empty on every migrated row. So the mentor enters it per
award — `full`, `partial`, or null for "not said" — and the profile shows
"fully funded" only where it is known.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def an_owner(engine: AsyncEngine, tag: str) -> tuple[object, dict[str, str]]:
    auth_id = uuid4()
    user = await make_user(engine, auth_id, f"funding-{tag}@example.com")
    return user, bearer(api_token(auth_id))


async def test_an_award_is_saved_with_its_funding(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await an_owner(db_engine, "create")

    created = await api_client.post(
        url(user, "awards"),
        json={"title": "Chevening", "institution": "FCDO", "funding": "full"},
        headers=headers,
    )
    (award,) = (await api_client.get(url(user, "awards"), headers=headers)).json()["data"]

    assert created.status_code == 201
    assert award["funding"] == "full"


async def test_funding_is_null_until_the_mentor_says(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await an_owner(db_engine, "unsaid")

    await api_client.post(
        url(user, "awards"), json={"title": "DAAD", "institution": "DAAD"}, headers=headers
    )
    (award,) = (await api_client.get(url(user, "awards"), headers=headers)).json()["data"]

    assert award["funding"] is None


async def test_a_patch_changes_clears_or_leaves_funding(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await an_owner(db_engine, "patch")
    award_id = (
        await api_client.post(
            url(user, "awards"),
            json={"title": "Fulbright", "institution": "US State", "funding": "partial"},
            headers=headers,
        )
    ).json()["id"]

    async def funding() -> object:
        (award,) = (await api_client.get(url(user, "awards"), headers=headers)).json()["data"]
        return award["funding"]

    await api_client.patch(url(user, f"awards/{award_id}"), json={"year": 2024}, headers=headers)
    assert await funding() == "partial", "a patch that did not send funding changed it"

    await api_client.patch(
        url(user, f"awards/{award_id}"), json={"funding": "full"}, headers=headers
    )
    assert await funding() == "full"

    await api_client.patch(url(user, f"awards/{award_id}"), json={"funding": None}, headers=headers)
    assert await funding() is None


@pytest.mark.parametrize("value", ["fully", "FULL", "half", ""])
async def test_an_unknown_funding_value_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: str
) -> None:
    user, headers = await an_owner(db_engine, f"bad-{uuid4().hex[:6]}")

    response = await api_client.post(
        url(user, "awards"),
        json={"title": "Commonwealth", "institution": "CSC", "funding": value},
        headers=headers,
    )

    # An empty string is normalised to null, which is "not said" and allowed.
    assert response.status_code == (201 if value == "" else 422)


async def test_the_public_profile_shows_funding(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "funding-public")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_awards (user_id, institution, title, year, funding) "
                "VALUES (:u, 'Oxford', 'Rhodes', 2024, 'full')"
            ),
            {"u": mentor},
        )

    (award,) = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()["scholarships"]

    assert award["funding"] == "full"


async def test_the_column_refuses_an_unknown_value(db_engine: AsyncEngine) -> None:
    """The CHECK, asked directly: the database holds the vocabulary too."""
    from sqlalchemy.exc import IntegrityError

    mentor = await make_public_mentor(db_engine, "funding-check")
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_awards (user_id, institution, title, funding) "
                    "VALUES (:u, 'X', 'Y', 'lots')"
                ),
                {"u": mentor},
            )


async def test_a_patch_with_an_unknown_value_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """At the boundary, as a 422 — not left to the CHECK, which would be a 500."""
    user, headers = await an_owner(db_engine, "patch-bad")
    award_id = (
        await api_client.post(
            url(user, "awards"), json={"title": "Erasmus", "institution": "EU"}, headers=headers
        )
    ).json()["id"]

    response = await api_client.patch(
        url(user, f"awards/{award_id}"), json={"funding": "half"}, headers=headers
    )

    assert response.status_code == 422

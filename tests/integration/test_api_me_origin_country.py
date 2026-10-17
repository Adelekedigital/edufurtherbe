"""Where a user is from, read back on their own `/me` (Mentee Onboarding v5, #6).

`PATCH /users/{id}/profile` has always taken `origin_country_id`; `/me` never
returned it, so onboarding could not prefill "Where are you from?" after a
refresh. `profile.origin_country` is the country as every other country in the
API reads (`CountryRef`: code and name), or null when unset.

**Only the owner reads it.** A mentee's origin never appears in a public or
mentor-facing response (PRD); `/me` is the one place `UserProfileRead` is
served. A mentor's origin was already public on their profile and card, as a
name, and stays exactly that.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session, make_bookable_mentor, token_for
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def country(engine: AsyncEngine, code: str) -> tuple[UUID, str]:
    """The catalogue's own id and name for ``code``."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, display_name FROM countries WHERE code = :c"), {"c": code}
            )
        ).one()
    return row.id, row.display_name


async def set_origin(engine: AsyncEngine, user: UUID, code: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, origin_country_id) "
                "VALUES (:u, (SELECT id FROM countries WHERE code = :c)) "
                "ON CONFLICT (user_id) DO UPDATE SET origin_country_id = EXCLUDED.origin_country_id"
            ),
            {"u": user, "c": code},
        )


async def me(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    response = await client.get("/api/v1/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_a_saved_origin_reads_back_on_me_with_the_catalogues_label(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "origin-set@example.com")
    headers = bearer(api_token(auth_id))
    ng_id, ng_name = await country(db_engine, "NG")

    saved = await api_client.patch(
        url(user, "profile"), headers=headers, json={"origin_country_id": str(ng_id)}
    )
    assert saved.status_code == 204, saved.text

    profile = (await me(api_client, headers))["profile"]
    assert profile["origin_country"] == {"code": "NG", "display_name": ng_name}  # type: ignore[index]


async def test_a_changed_origin_reads_the_new_country(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "origin-change@example.com")
    headers = bearer(api_token(auth_id))
    await set_origin(db_engine, user, "NG")
    gh_id, gh_name = await country(db_engine, "GH")

    await api_client.patch(
        url(user, "profile"), headers=headers, json={"origin_country_id": str(gh_id)}
    )

    profile = (await me(api_client, headers))["profile"]
    assert profile["origin_country"] == {"code": "GH", "display_name": gh_name}  # type: ignore[index]


async def test_an_unset_origin_is_null_and_still_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "origin-unset@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.patch(url(user, "profile"), headers=headers, json={"about_me": "Hi"})

    profile = (await me(api_client, headers))["profile"]
    assert "origin_country" in profile  # type: ignore[operator]
    assert profile["origin_country"] is None  # type: ignore[index]


async def test_a_cleared_origin_reads_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, "origin-clear@example.com")
    headers = bearer(api_token(auth_id))
    await set_origin(db_engine, user, "NG")

    cleared = await api_client.patch(
        url(user, "profile"), headers=headers, json={"origin_country_id": None}
    )
    assert cleared.status_code == 204, cleared.text

    profile = (await me(api_client, headers))["profile"]
    assert profile["origin_country"] is None  # type: ignore[index]


async def test_me_reads_only_the_callers_own_origin(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two users, two origins: each `/me` names its own."""
    first, second = uuid4(), uuid4()
    first_user = await make_user(db_engine, first, "origin-first@example.com")
    second_user = await make_user(db_engine, second, "origin-second@example.com")
    await set_origin(db_engine, first_user, "NG")
    await set_origin(db_engine, second_user, "KE")

    ours = (await me(api_client, bearer(api_token(first))))["profile"]
    theirs = (await me(api_client, bearer(api_token(second))))["profile"]
    assert ours["origin_country"]["code"] == "NG"  # type: ignore[index]
    assert theirs["origin_country"]["code"] == "KE"  # type: ignore[index]


async def test_the_country_ref_never_reaches_the_public_profile_or_cards(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor's origin was public as a name and stays exactly that.

    The owner's `{code, display_name}` ref and the `profile` object are `/me`
    only; the public profile and every card keep their shipped shape.
    """
    mentor = await make_bookable_mentor(db_engine, "origin-public")
    await set_origin(db_engine, mentor, "NG")
    _, ng_name = await country(db_engine, "NG")

    public = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()
    cards = (await api_client.get("/api/v1/mentors")).json()["data"]
    card = next(c for c in cards if c["id"] == str(mentor))

    for body in (public, card):
        assert "profile" not in body
        assert body["origin_country"] == ng_name

    owner = (await me(api_client, bearer(await token_for(db_engine, mentor))))["profile"]
    assert owner["origin_country"] == {"code": "NG", "display_name": ng_name}  # type: ignore[index]


async def test_a_mentees_origin_never_reaches_their_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The mentor reads the session's party card; the mentee's origin is not on it."""
    mentor = await make_bookable_mentor(db_engine, "origin-party")
    auth_id = uuid4()
    mentee = await make_user(db_engine, auth_id, "origin-party-mentee@example.com")
    await set_origin(db_engine, mentee, "NG")
    session_id = await add_session(db_engine, mentor, mentee=mentee, status="confirmed")

    read = await api_client.get(
        f"/api/v1/sessions/{session_id}", headers=bearer(await token_for(db_engine, mentor))
    )
    assert read.status_code == 200, read.text
    _, ng_name = await country(db_engine, "NG")
    assert not [key for key in read.json()["mentee"] if "origin" in key]
    assert ng_name not in read.text

    own = (await me(api_client, bearer(api_token(auth_id))))["profile"]
    assert own["origin_country"]["code"] == "NG"  # type: ignore[index]

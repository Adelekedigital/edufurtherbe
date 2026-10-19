"""Where a user lives now, written on the profile and read back on `/me` (Mentee Settings PRD).

`current_country_id` was once writable and read by nothing, which looked
recorded and was not (see `UserProfileWrite`). It is writable again because it
is now returned: `profile.current_country`, `{code, display_name}` like every
other country here, only to the owner. An id that names no country is a `422`
at the field rather than the foreign-key 500 it used to be, and `origin_country_id`
follows the same rule.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_me_origin_country import country, me
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _owner(engine: AsyncEngine, email: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user = await make_user(engine, auth_id, email)
    return user, bearer(api_token(auth_id))


async def test_a_saved_current_country_reads_back_and_null_clears_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine, "lives@example.com")
    gh_id, gh_name = await country(db_engine, "GH")

    saved = await api_client.patch(
        url(user, "profile"), headers=headers, json={"current_country_id": str(gh_id)}
    )
    assert saved.status_code == 204, saved.text
    profile = (await me(api_client, headers))["profile"]
    assert profile["current_country"] == {"code": "GH", "display_name": gh_name}  # type: ignore[index]
    # A different field, so the two cannot be read off one join by mistake.
    assert profile["origin_country"] is None  # type: ignore[index]

    cleared = await api_client.patch(
        url(user, "profile"), headers=headers, json={"current_country_id": None}
    )
    assert cleared.status_code == 204, cleared.text
    assert (await me(api_client, headers))["profile"]["current_country"] is None  # type: ignore[index]


async def test_a_new_user_reads_no_current_country(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await _owner(db_engine, "fresh@example.com")

    body = await me(api_client, headers)

    assert body["profile"] is None or body["profile"]["current_country"] is None  # type: ignore[index]


@pytest.mark.parametrize("field", ["current_country_id", "origin_country_id"])
async def test_an_unknown_country_is_a_422_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, field: str
) -> None:
    user, headers = await _owner(db_engine, f"unknown-{field}@example.com")

    refused = await api_client.patch(
        url(user, "profile"),
        headers=headers,
        json={field: str(uuid4()), "about_me": "Should not be saved."},
    )

    assert refused.status_code == 422, refused.text
    assert [e["pointer"] for e in refused.json()["errors"]] == [f"/{field}"]
    profile = (await me(api_client, headers))["profile"]
    assert profile is None or profile["about_me"] is None  # type: ignore[index]


async def test_a_known_origin_is_still_accepted_beside_a_current_country(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, headers = await _owner(db_engine, "both@example.com")
    ng_id, ng_name = await country(db_engine, "NG")
    gb_id, gb_name = await country(db_engine, "GB")

    saved = await api_client.patch(
        url(user, "profile"),
        headers=headers,
        json={"origin_country_id": str(ng_id), "current_country_id": str(gb_id)},
    )

    assert saved.status_code == 204, saved.text
    profile = (await me(api_client, headers))["profile"]
    assert profile["origin_country"] == {"code": "NG", "display_name": ng_name}  # type: ignore[index]
    assert profile["current_country"] == {"code": "GB", "display_name": gb_name}  # type: ignore[index]

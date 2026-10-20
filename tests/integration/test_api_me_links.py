"""`/me/links`: a user's own links (decision 275).

One per platform except websites (three), every write scoped to the caller,
and every read (`/me/links`, `/me`, the public profile) answering from the
one table.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor, token_for
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

LINKS = "/api/v1/me/links"


async def a_user(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"links-{tag}-{uuid4().hex[:6]}@example.com")
    return user_id, bearer(api_token(auth_id))


async def add(
    client: httpx.AsyncClient, headers: dict[str, str], platform: str, value: str
) -> httpx.Response:
    return await client.post(LINKS, json={"platform": platform, "value": value}, headers=headers)


# --- Create and read ---------------------------------------------------------


async def test_a_link_is_stored_canonical_and_read_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "order")

    first = await add(api_client, headers, "instagram", "instagram.com/ada.l/")
    second = await add(api_client, headers, "website", "http://Ada.Dev/blog#top")

    assert first.status_code == 201, first.text
    body = first.json()
    assert body == {
        "id": body["id"],
        "platform": "instagram",
        "value": "ada.l",
        "url": "https://www.instagram.com/ada.l",
        "position": 0,
    }
    assert first.headers["location"] == f"{LINKS}/{body['id']}"
    assert second.json()["url"] == "https://ada.dev/blog"
    assert second.json()["position"] == 1
    listed = (await api_client.get(LINKS, headers=headers)).json()
    assert [link["platform"] for link in listed["data"]] == ["instagram", "website"]
    assert listed["next_cursor"] is None


async def test_none_is_an_empty_list(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    _, headers = await a_user(db_engine, "empty")

    listed = await api_client.get(LINKS, headers=headers)

    assert listed.status_code == 200
    assert listed.json()["data"] == []


async def test_a_value_that_is_not_on_the_platform_is_a_422_at_value(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "bad")

    response = await add(api_client, headers, "tiktok", "https://evil.com/@ada")

    assert response.status_code == 422
    assert [e["pointer"] for e in response.json()["errors"]] == ["/value"]
    assert (await api_client.get(LINKS, headers=headers)).json()["data"] == []


@pytest.mark.parametrize("body", [{"platform": "linkedin", "value": "ada"}, {"value": "ada"}])
async def test_linkedin_and_a_missing_platform_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, str]
) -> None:
    _, headers = await a_user(db_engine, "noplat")

    response = await api_client.post(LINKS, json=body, headers=headers)

    assert response.status_code == 422
    assert "/platform" in [e["pointer"] for e in response.json()["errors"]]


async def test_a_second_link_on_a_platform_is_a_409(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "dup")
    assert (await add(api_client, headers, "x", "ada")).status_code == 201

    response = await add(api_client, headers, "x", "bo")

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/link-platform-taken"
    assert len((await api_client.get(LINKS, headers=headers)).json()["data"]) == 1


async def test_three_websites_fit_and_a_fourth_is_a_409(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "web")
    for n in range(3):
        assert (await add(api_client, headers, "website", f"ada{n}.dev")).status_code == 201

    response = await add(api_client, headers, "website", "ada3.dev")

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/website-limit-reached"
    assert response.json()["limit"] == 3


async def test_websites_arriving_together_never_exceed_three(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The count is taken under a per-user lock, so parallel adds cannot all pass it."""
    _, headers = await a_user(db_engine, "race")
    await add(api_client, headers, "website", "one.dev")

    responses = await asyncio.gather(
        *(add(api_client, headers, "website", f"site{n}.dev") for n in range(4))
    )

    assert sorted(r.status_code for r in responses) == [201, 201, 409, 409]
    assert len((await api_client.get(LINKS, headers=headers)).json()["data"]) == 3


async def test_the_same_platform_arriving_twice_together_lands_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "race-x")

    responses = await asyncio.gather(*(add(api_client, headers, "x", f"ada{n}") for n in range(3)))

    assert sorted(r.status_code for r in responses) == [201, 409, 409]


async def test_unauthenticated_is_a_401(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(LINKS)).status_code == 401
    assert (await api_client.post(LINKS, json={"platform": "x", "value": "a"})).status_code == 401


# --- Change and delete -------------------------------------------------------


async def test_a_link_changes_its_value_but_not_its_platform(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "patch")
    link = (await add(api_client, headers, "facebook", "ada.lovelace")).json()

    changed = await api_client.patch(
        f"{LINKS}/{link['id']}", json={"value": "profile.php?id=42"}, headers=headers
    )
    moved = await api_client.patch(
        f"{LINKS}/{link['id']}", json={"value": "ada", "platform": "x"}, headers=headers
    )
    refused = await api_client.patch(
        f"{LINKS}/{link['id']}", json={"value": "https://evil.com/x"}, headers=headers
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["url"] == "https://www.facebook.com/profile.php?id=42"
    assert changed.json()["position"] == link["position"]
    assert moved.status_code == 422
    assert refused.status_code == 422
    assert [e["pointer"] for e in refused.json()["errors"]] == ["/value"]


async def test_somebody_elses_link_is_a_404_and_stays(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, owner = await a_user(db_engine, "owner")
    _, stranger = await a_user(db_engine, "stranger")
    link = (await add(api_client, owner, "x", "ada")).json()
    target = f"{LINKS}/{link['id']}"

    patched = await api_client.patch(target, json={"value": "evil"}, headers=stranger)
    deleted = await api_client.delete(target, headers=stranger)

    assert (patched.status_code, deleted.status_code) == (404, 404)
    kept = (await api_client.get(LINKS, headers=owner)).json()["data"]
    assert [(k["id"], k["url"]) for k in kept] == [(link["id"], "https://x.com/ada")]
    assert (await api_client.get(LINKS, headers=stranger)).json()["data"] == []


async def test_a_deleted_link_is_gone_and_a_second_delete_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "delete")
    keep = (await add(api_client, headers, "x", "ada")).json()
    drop = (await add(api_client, headers, "tiktok", "@ada_l")).json()

    first = await api_client.delete(f"{LINKS}/{drop['id']}", headers=headers)
    second = await api_client.delete(f"{LINKS}/{drop['id']}", headers=headers)

    assert (first.status_code, second.status_code) == (204, 404)
    remaining = (await api_client.get(LINKS, headers=headers)).json()["data"]
    assert [link["id"] for link in remaining] == [keep["id"]]


async def test_an_unknown_id_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_user(db_engine, "unknown")

    response = await api_client.patch(f"{LINKS}/{uuid4()}", json={"value": "a"}, headers=headers)

    assert response.status_code == 404


# --- The removed profile fields (#505) ---------------------------------------


async def test_the_old_x_and_youtube_fields_are_gone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """#505: links are `links` only. Sent to the profile PATCH, the old fields
    are ignored like any unknown field; no read carries them."""
    user_id, headers = await a_user(db_engine, "legacy-gone")
    await api_client.patch(url(user_id, "profile"), json={"about_me": "Hi."}, headers=headers)

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"social_twitter": "@ada", "social_youtube": "@ada"},
        headers=headers,
    )
    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert response.status_code == 204, response.text
    assert me["links"] == []
    assert not {"social_twitter", "social_youtube"} & me["profile"].keys()


async def test_me_reads_the_links(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    user_id, headers = await a_user(db_engine, "me")
    await api_client.patch(url(user_id, "profile"), json={"about_me": "Hi."}, headers=headers)
    await add(api_client, headers, "youtube", "@ada")
    await add(api_client, headers, "instagram", "ada.l")

    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert [(k["platform"], k["value"]) for k in me["links"]] == [
        ("youtube", "ada"),
        ("instagram", "ada.l"),
    ]
    assert me["links"] == (await api_client.get(LINKS, headers=headers)).json()["data"]


async def test_the_public_profile_shows_platform_and_url_only(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "links-public")
    headers = bearer(await token_for(db_engine, mentor))
    await add(api_client, headers, "x", "ada")
    await add(api_client, headers, "website", "ada.dev")

    body = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()

    assert body["links"] == [
        {"platform": "x", "url": "https://x.com/ada"},
        {"platform": "website", "url": "https://ada.dev"},
    ]
    assert not {"social_twitter", "social_youtube"} & body.keys()


async def test_a_link_the_rule_no_longer_reads_stays_the_owners_to_delete(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """It still holds the platform's slot, so the owner must see it to remove it;
    nobody else is ever shown it (#182)."""
    mentor = await make_bookable_mentor(db_engine, "links-unreadable")
    headers = bearer(await token_for(db_engine, mentor))
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_links (user_id, platform, url) "
                "VALUES (:u, 'x', 'https://evil.example/ada')"
            ),
            {"u": mentor},
        )

    own = (await api_client.get(LINKS, headers=headers)).json()["data"]
    public = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()
    deleted = await api_client.delete(f"{LINKS}/{own[0]['id']}", headers=headers)

    assert [(k["platform"], k["url"]) for k in own] == [("x", "https://evil.example/ada")]
    assert public["links"] == []
    assert deleted.status_code == 204
    assert (await add(api_client, headers, "x", "ada")).status_code == 201

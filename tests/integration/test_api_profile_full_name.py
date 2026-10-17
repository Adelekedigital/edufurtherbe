"""A user saves their name as one string, and it is split into first and last.

The sign-up screen asks for a full name once (decision 258). The first word is
the first name and the rest the last name; each part follows the name rule
`first_name` and `last_name` already follow (#213).
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_profile_owner_edits import a_user, focus_of, names_of, pointers
from tests.integration.test_api_writes import url

from app.api.schemas.profile import MAX_NAME_LENGTH

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def messages(response: httpx.Response) -> list[str]:
    return [e["message"] for e in response.json()["errors"] if e["pointer"] == "/full_name"]


async def test_a_full_name_is_split_on_the_first_word(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "full")

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"full_name": "  Grace  Brewster   Hopper "},
        headers=headers,
    )
    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert response.status_code == 204
    assert await names_of(db_engine, user_id) == ("Grace", "Brewster Hopper")
    assert (me["first_name"], me["last_name"]) == ("Grace", "Brewster Hopper")


async def test_two_words_are_a_first_and_a_last_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "two")

    await api_client.patch(
        url(user_id, "profile"), json={"full_name": "Damilola Ogunleye"}, headers=headers
    )

    assert await names_of(db_engine, user_id) == ("Damilola", "Ogunleye")


@pytest.mark.parametrize("value", [None, "", "   ", "Grace", "  Grace  "])
async def test_a_blank_or_single_word_full_name_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: object
) -> None:
    user_id, headers = await a_user(db_engine, "one-word")

    response = await api_client.patch(
        url(user_id, "profile"), json={"full_name": value}, headers=headers
    )

    assert response.status_code == 422
    assert "/full_name" in pointers(response)
    assert await names_of(db_engine, user_id) == ("Ada", None)


@pytest.mark.parametrize(
    "value",
    [
        "Grace\nHopper",
        "Grace\tHopper",
        "Grace\u202eHopper x",
        "Grace \u200b",
        "Grace ---",
        "--- Hopper",
    ],
    ids=[
        "newline",
        "tab",
        "rtl-override",
        "zero-width-part",
        "letterless-last",
        "letterless-first",
    ],
)
async def test_an_invisible_or_letterless_part_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: str
) -> None:
    """A line break is refused, not read as the space between two names: the
    check runs on the whole string before it is split."""
    user_id, headers = await a_user(db_engine, "bad")

    response = await api_client.patch(
        url(user_id, "profile"), json={"full_name": value}, headers=headers
    )

    assert response.status_code == 422
    assert "/full_name" in pointers(response)
    assert await names_of(db_engine, user_id) == ("Ada", None)


async def test_one_word_and_bad_characters_say_different_things(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "messages")
    profile = url(user_id, "profile")

    one_word = await api_client.patch(profile, json={"full_name": "Grace"}, headers=headers)
    hidden = await api_client.patch(profile, json={"full_name": "Grace\nHopper"}, headers=headers)

    assert messages(one_word) and messages(hidden)
    assert messages(one_word) != messages(hidden)


@pytest.mark.parametrize("other", ["first_name", "last_name"])
async def test_a_full_name_with_a_part_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, other: str
) -> None:
    """Two spellings of one name in one request: nothing says which wins."""
    user_id, headers = await a_user(db_engine, "both")

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"full_name": "Grace Hopper", other: "Augusta"},
        headers=headers,
    )

    assert response.status_code == 422
    assert "/full_name" in pointers(response)
    assert await names_of(db_engine, user_id) == ("Ada", None)


async def test_each_part_keeps_the_name_limit(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "limit")
    at_limit = "y" * MAX_NAME_LENGTH

    kept = await api_client.patch(
        url(user_id, "profile"), json={"full_name": f"{at_limit} {at_limit}"}, headers=headers
    )
    over = await api_client.patch(
        url(user_id, "profile"), json={"full_name": f"Grace {at_limit}z"}, headers=headers
    )

    assert kept.status_code == 204
    assert over.status_code == 422
    assert "/full_name" in pointers(over)
    assert await names_of(db_engine, user_id) == (at_limit, at_limit)


async def test_a_full_name_is_composed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "nfc")

    await api_client.patch(
        url(user_id, "profile"), json={"full_name": "Rene\u0301e Nu\u00f1ez"}, headers=headers
    )

    assert await names_of(db_engine, user_id) == ("Ren\u00e9e", "Nu\u00f1ez")


async def test_a_full_name_alone_creates_no_empty_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "noprofile")

    await api_client.patch(
        url(user_id, "profile"), json={"full_name": "Grace Hopper"}, headers=headers
    )

    assert await focus_of(db_engine, user_id) is None


async def test_another_users_full_name_cannot_be_written(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    victim, _ = await a_user(db_engine, "victim")
    _, headers = await a_user(db_engine, "attacker")

    response = await api_client.patch(
        url(victim, "profile"), json={"full_name": "Mallory Smith"}, headers=headers
    )

    assert response.status_code == 404
    assert await names_of(db_engine, victim) == ("Ada", None)

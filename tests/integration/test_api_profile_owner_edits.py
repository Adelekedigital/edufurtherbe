"""A user edits their own name, and a mentor chooses where their photo is centred.

Owner decisions 2026-09-29, from the profile-page audit: the design's owner edit
mode renames and re-crops, and neither was writable. First sign-in creates an
account with **no name at all** (`first_sign_in.py`), so until now a new user
could never have one. Both ride on `PATCH /users/{id}/profile`.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_writes import make_user, url

from app.api.schemas.profile import MAX_NAME_LENGTH
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

AVATAR = "https://project.supabase.co/storage/v1/object/public/avatars/x.jpg"


async def a_user(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"owner-{tag}-{uuid4().hex[:6]}@example.com")
    return user_id, bearer(api_token(auth_id))


async def names_of(engine: AsyncEngine, user_id: UUID) -> tuple[object, object]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT first_name, last_name FROM users WHERE id = :u"), {"u": user_id}
            )
        ).one()
    return row[0], row[1]


async def with_avatar(engine: AsyncEngine, user_id: UUID) -> None:
    """A stored photo with a detected focus, as an upload would leave it."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, avatar_url, avatar_focus_x, "
                " avatar_focus_y, avatar_focus_source) "
                "VALUES (:u, :a, 0.5, 0.4, 'detected')"
            ),
            {"u": user_id, "a": AVATAR},
        )


async def focus_of(engine: AsyncEngine, user_id: UUID) -> tuple[object, object, object] | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT avatar_focus_x, avatar_focus_y, avatar_focus_source "
                    "FROM user_profiles WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).first()
    return None if row is None else (row[0], row[1], row[2])


def pointers(response: httpx.Response) -> set[str]:
    return {e["pointer"] for e in response.json()["errors"]}


# --------------------------------------------------------------------------
# The name
# --------------------------------------------------------------------------


async def test_a_user_saves_their_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "name")

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"first_name": "  Grace ", "last_name": "Hopper"},
        headers=headers,
    )
    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert response.status_code == 204
    assert await names_of(db_engine, user_id) == ("Grace", "Hopper")
    assert (me["first_name"], me["last_name"]) == ("Grace", "Hopper")


async def test_one_name_alone_leaves_the_other(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "one")

    await api_client.patch(url(user_id, "profile"), json={"last_name": "Lovelace"}, headers=headers)

    assert await names_of(db_engine, user_id) == ("Ada", "Lovelace")


@pytest.mark.parametrize("value", [None, "", "   ", "x" * (MAX_NAME_LENGTH + 1)])
@pytest.mark.parametrize("field", ["first_name", "last_name"])
async def test_a_blank_or_overlong_name_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, field: str, value: object
) -> None:
    """A name can be changed, never cleared: blank is a `422`, not a deletion."""
    user_id, headers = await a_user(db_engine, "blank")

    response = await api_client.patch(url(user_id, "profile"), json={field: value}, headers=headers)

    assert response.status_code == 422
    assert f"/{field}" in pointers(response)
    assert await names_of(db_engine, user_id) == ("Ada", None)


@pytest.mark.parametrize(
    "value",
    [
        "\u200b\u200b\u200b",
        "Ada\u202eecalvol",
        "Ada\nLovelace",
        "\u00a0",
        "---",
        "\ue000",
        "\u200dAda",
        "Ada\u200c",
        "Ada \u200d Lovelace",
        "Ad\u200d\u200da",
        "Ad\u200c\u202ea",
        "Ad\u2066a",
    ],
    ids=[
        "zero-width-only",
        "rtl-override",
        "embedded-newline",
        "nbsp-only",
        "no-letter",
        "private-use",
        "leading-zwj",
        "trailing-zwnj",
        "lone-zwj-between-spaces",
        "doubled-zwj",
        "zwnj-beside-rtl-override",
        "isolate",
    ],
)
async def test_an_invisible_or_letterless_name_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: str
) -> None:
    """Names reach public cards, email variables and the call's display name, so a
    control or format character is refused rather than stored, or stripped.
    (NUL is the one exception: `Normalised` removes it from every field first.)"""
    user_id, headers = await a_user(db_engine, "invisible")

    response = await api_client.patch(
        url(user_id, "profile"), json={"first_name": value}, headers=headers
    )

    assert response.status_code == 422
    assert "/first_name" in pointers(response)
    assert await names_of(db_engine, user_id) == ("Ada", None)


@pytest.mark.parametrize(
    "value",
    [
        "Ren\u00e9e",
        "Nu\u00f1ez",
        "\u0639\u0627\u0626\u0634\u0629",
        "\u674e\u534e",
        "Mary-Jane",
        "O'Brien",
        "Jean Paul",
        "\u062d\u0633\u06cc\u0646\u200c\u0632\u0627\u062f\u0647",
        "\u0dc1\u0dca\u200d\u0dbb\u0dd3",
    ],
    ids=[
        "accent",
        "tilde",
        "arabic",
        "cjk",
        "hyphen",
        "apostrophe",
        "space",
        "persian-zwnj",
        "sinhala-zwj",
    ],
)
async def test_a_real_name_in_any_script_is_kept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: str
) -> None:
    user_id, headers = await a_user(db_engine, "script")

    response = await api_client.patch(
        url(user_id, "profile"), json={"last_name": value}, headers=headers
    )

    assert response.status_code == 204
    assert await names_of(db_engine, user_id) == ("Ada", value)


async def test_a_decomposed_accent_is_stored_composed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`e` + combining acute and `\u00e9` are one name; store one spelling of it."""
    user_id, headers = await a_user(db_engine, "nfc")

    await api_client.patch(
        url(user_id, "profile"), json={"first_name": "Rene\u0301e"}, headers=headers
    )

    assert await names_of(db_engine, user_id) == ("Ren\u00e9e", None)


async def test_the_length_is_counted_after_composing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """NFC can lengthen a name: U+0958 has no composed form, so it is stored as two
    code points. The limit applies to what is stored, not to what was sent."""
    user_id, headers = await a_user(db_engine, "nfc-length")

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"first_name": "\u0958" * MAX_NAME_LENGTH},
        headers=headers,
    )

    assert response.status_code == 422
    assert "/first_name" in pointers(response)
    assert await names_of(db_engine, user_id) == ("Ada", None)


async def test_a_name_at_the_limit_is_kept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "limit")

    response = await api_client.patch(
        url(user_id, "profile"), json={"first_name": "y" * MAX_NAME_LENGTH}, headers=headers
    )

    assert response.status_code == 204
    assert await names_of(db_engine, user_id) == ("y" * MAX_NAME_LENGTH, None)


async def test_renaming_keeps_the_profile_link(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The slug is the legacy public handle; a rename must not break a shared link."""
    user_id, headers = await a_user(db_engine, "slug")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET slug = 'ada-lovelace' WHERE id = :u"), {"u": user_id}
        )

    await api_client.patch(url(user_id, "profile"), json={"first_name": "Augusta"}, headers=headers)

    async with db_engine.connect() as conn:
        slug = (
            await conn.execute(text("SELECT slug FROM users WHERE id = :u"), {"u": user_id})
        ).scalar_one()
    assert slug == "ada-lovelace"
    assert await names_of(db_engine, user_id) == ("Augusta", None)


async def test_a_name_alone_creates_no_empty_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A name lives on `users`; saving one is not starting a profile."""
    user_id, headers = await a_user(db_engine, "noprofile")

    await api_client.patch(url(user_id, "profile"), json={"first_name": "Grace"}, headers=headers)

    assert await focus_of(db_engine, user_id) is None


async def test_another_users_name_cannot_be_written(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    victim, _ = await a_user(db_engine, "victim")
    _, headers = await a_user(db_engine, "attacker")

    response = await api_client.patch(
        url(victim, "profile"), json={"first_name": "Mallory"}, headers=headers
    )

    assert response.status_code == 404
    assert await names_of(db_engine, victim) == ("Ada", None)


# --------------------------------------------------------------------------
# The chosen crop
# --------------------------------------------------------------------------


async def test_a_mentor_chooses_where_their_photo_is_centred(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "crop")
    await with_avatar(db_engine, user_id)

    response = await api_client.patch(
        url(user_id, "profile"), json={"avatar_focus": {"x": 0.25, "y": 0.1}}, headers=headers
    )
    me = (await api_client.get("/api/v1/me", headers=headers)).json()

    assert response.status_code == 204
    assert await focus_of(db_engine, user_id) == (Decimal("0.250"), Decimal("0.100"), "chosen")
    assert me["profile"]["avatar_focus"] == {"x": 0.25, "y": 0.1}


async def test_a_chosen_crop_is_kept_to_the_stored_precision(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "precision")
    await with_avatar(db_engine, user_id)

    await api_client.patch(
        url(user_id, "profile"), json={"avatar_focus": {"x": 0.12345, "y": 1}}, headers=headers
    )

    assert await focus_of(db_engine, user_id) == (Decimal("0.123"), Decimal("1.000"), "chosen")


async def test_there_is_nothing_to_crop_without_a_photo(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Refused, and the rest of the request with it: one write, all or nothing."""
    user_id, headers = await a_user(db_engine, "nophoto")

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"avatar_focus": {"x": 0.5, "y": 0.5}, "first_name": "Grace"},
        headers=headers,
    )

    assert response.status_code == 422
    assert "/avatar_focus" in pointers(response)
    assert await focus_of(db_engine, user_id) is None
    assert await names_of(db_engine, user_id) == ("Ada", None)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"x": -0.1, "y": 0.5},
        {"x": 0.5, "y": 1.01},
        {"x": 0.5},
        {"x": 0.5, "y": 0.5, "source": "detected"},
    ],
    ids=["null", "left-of-the-image", "below-the-image", "half-a-point", "extra-key"],
)
async def test_a_focus_outside_the_image_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, value: object
) -> None:
    user_id, headers = await a_user(db_engine, "bad")
    await with_avatar(db_engine, user_id)

    response = await api_client.patch(
        url(user_id, "profile"), json={"avatar_focus": value}, headers=headers
    )

    assert response.status_code == 422
    assert any(p.startswith("/avatar_focus") for p in pointers(response))
    assert await focus_of(db_engine, user_id) == (Decimal("0.500"), Decimal("0.400"), "detected")


async def test_leaving_the_focus_out_leaves_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await a_user(db_engine, "omit")
    await with_avatar(db_engine, user_id)

    await api_client.patch(url(user_id, "profile"), json={"about_me": "Hi"}, headers=headers)

    assert await focus_of(db_engine, user_id) == (Decimal("0.500"), Decimal("0.400"), "detected")


async def test_another_users_crop_cannot_be_chosen(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    victim, _ = await a_user(db_engine, "cropvictim")
    await with_avatar(db_engine, victim)
    _, headers = await a_user(db_engine, "cropattacker")

    response = await api_client.patch(
        url(victim, "profile"), json={"avatar_focus": {"x": 0.9, "y": 0.9}}, headers=headers
    )

    assert response.status_code == 404
    assert await focus_of(db_engine, victim) == (Decimal("0.500"), Decimal("0.400"), "detected")

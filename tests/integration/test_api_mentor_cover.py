"""A mentor's cover: a colour and an art style, when there is no banner image.

Frontend #19. `cover_color` is one of the design's twelve keys, in the design's
order, or null for the automatic colour (the frontend hashes the id over the
same list). `cover_art` is `none`, `icons`, `pattern` or `single`, default
`none`. A `banner_url`, when set, still wins — that is the frontend's rule and
needs nothing here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor
from tests.integration.test_api_writes import make_user, url

from app.domain.enums import CoverArt, CoverColor
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DESIGN_ORDER = [
    "sky", "ice", "aqua", "mint", "sage", "lemon",
    "sand", "peach", "blush", "rose", "lilac", "mist",
]  # fmt: skip


async def owner(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    user_id = await make_user(engine, auth_id, f"cover-{tag}@example.com")
    return user_id, bearer(api_token(auth_id))


async def stored(engine: AsyncEngine, user_id: UUID) -> tuple[object, object]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT cover_color, cover_art FROM user_profiles WHERE user_id = :u"),
                {"u": user_id},
            )
        ).one()
    return row.cover_color, row.cover_art


def test_the_colours_are_the_designs_in_its_order() -> None:
    """The frontend hashes over this list in this order; the spec must agree."""
    assert [c.value for c in CoverColor] == DESIGN_ORDER
    assert [a.value for a in CoverArt] == ["none", "icons", "pattern", "single"]


async def test_a_mentor_chooses_a_colour_and_art(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await owner(db_engine, "choose")

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"cover_color": "sage", "cover_art": "pattern"},
        headers=headers,
    )

    assert response.status_code == 204
    assert await stored(db_engine, user_id) == ("sage", "pattern")


async def test_null_colour_is_automatic_and_art_defaults_to_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await owner(db_engine, "defaults")
    await api_client.patch(url(user_id, "profile"), json={"cover_color": "rose"}, headers=headers)

    response = await api_client.patch(
        url(user_id, "profile"), json={"cover_color": None}, headers=headers
    )

    assert response.status_code == 204
    assert await stored(db_engine, user_id) == (None, "none")


@pytest.mark.parametrize(
    "payload",
    [{"cover_color": "teal"}, {"cover_art": "stripes"}, {"cover_art": None}],
)
async def test_anything_else_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, payload: dict[str, object]
) -> None:
    user_id, headers = await owner(db_engine, f"refuse-{uuid4().hex[:8]}")

    response = await api_client.patch(url(user_id, "profile"), json=payload, headers=headers)

    assert response.status_code == 422


async def test_leaving_a_field_out_leaves_it_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await owner(db_engine, "partial")
    await api_client.patch(
        url(user_id, "profile"),
        json={"cover_color": "lemon", "cover_art": "icons"},
        headers=headers,
    )

    await api_client.patch(url(user_id, "profile"), json={"about_me": "Hello"}, headers=headers)

    assert await stored(db_engine, user_id) == ("lemon", "icons")


async def test_the_public_profile_and_me_carry_the_cover(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "cover-public")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, cover_color, cover_art) "
                "VALUES (:u, 'mist', 'single')"
            ),
            {"u": mentor},
        )
        auth_id = (
            await conn.execute(text("SELECT auth_id FROM users WHERE id = :u"), {"u": mentor})
        ).scalar_one()
        await conn.execute(
            text("UPDATE users SET email = 'cover-public@example.com' WHERE id = :u"),
            {"u": mentor},
        )

    public = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()
    me = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()

    assert (public["cover_color"], public["cover_art"]) == ("mist", "single")
    assert (me["profile"]["cover_color"], me["profile"]["cover_art"]) == ("mist", "single")


async def test_a_mentor_with_no_profile_row_reads_the_defaults(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "cover-none")

    public = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()

    assert (public["cover_color"], public["cover_art"]) == (None, "none")

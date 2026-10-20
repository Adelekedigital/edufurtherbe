"""Social links through the API: canonical on the way in and on the way out (#182).

The client renders these and never parses them, so every response carries the
canonical `https://` form or `null` — including for a value stored before the
rule existed, which no write ever normalised.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor
from tests.integration.test_api_writes import make_user, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def stored(engine: AsyncEngine, user_id: UUID) -> dict[str, object]:
    """LinkedIn from its column; X and YouTube from `user_links` (decision 275)."""
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT p.social_linkedin, "
                " (SELECT url FROM user_links WHERE user_id = u.id AND platform = 'x') "
                "   AS social_twitter, "
                " (SELECT url FROM user_links WHERE user_id = u.id AND platform = 'youtube') "
                "   AS social_youtube "
                "FROM users u LEFT JOIN user_profiles p ON p.user_id = u.id WHERE u.id = :u"
            ),
            {"u": user_id},
        )
        return dict(row.mappings().one())


async def store_legacy(
    engine: AsyncEngine, user_id: UUID, linkedin: str, x: str | None, yt: str | None
) -> None:
    """Values as the old system left them, written past the boundary: LinkedIn
    in its column, X and YouTube as `user_links` rows (any `https://` link the
    table's CHECK admits, canonical or not)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, social_linkedin) VALUES (:u, :l) "
                "ON CONFLICT (user_id) DO UPDATE SET social_linkedin = excluded.social_linkedin"
            ),
            {"u": user_id, "l": linkedin},
        )
        for position, (platform, link) in enumerate((("x", x), ("youtube", yt))):
            if link is not None:
                await conn.execute(
                    text(
                        "INSERT INTO user_links (user_id, platform, url, position) "
                        "VALUES (:u, :p, :url, :pos)"
                    ),
                    {"u": user_id, "p": platform, "url": link, "pos": position},
                )


async def test_a_handle_or_a_link_is_stored_canonical(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "social-write@example.com")
    headers = bearer(api_token(auth_id))

    response = await api_client.patch(
        url(user_id, "profile"),
        json={
            "social_linkedin": "linkedin.com/in/ada-lovelace?trk=x",
            "social_twitter": "@ada",
            "social_youtube": "https://m.youtube.com/@ada.codes/videos",
        },
        headers=headers,
    )

    assert response.status_code == 204
    expected = {
        "social_linkedin": "https://www.linkedin.com/in/ada-lovelace",
        "social_twitter": "https://x.com/ada",
        "social_youtube": "https://www.youtube.com/@ada.codes",
    }
    assert await stored(db_engine, user_id) == expected
    me = (await api_client.get("/api/v1/me", headers=headers)).json()
    assert {k: me["profile"][k] for k in expected} == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("social_linkedin", "https://linkedin.com.evil.com/in/ada"),
        ("social_linkedin", "https://linkedin.com@evil.com/in/ada"),
        ("social_twitter", "javascript:alert(1)"),
        ("social_youtube", "https://evil.com/?u=youtube.com/@ada"),
    ],
)
async def test_anything_else_is_refused_by_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, field: str, value: str
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"social-422-{uuid4().hex[:6]}@example.com")

    response = await api_client.patch(
        url(user_id, "profile"), json={field: value}, headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 422
    assert field in response.text
    async with db_engine.connect() as conn:
        written = await conn.execute(
            text("SELECT count(*) FROM user_profiles WHERE user_id = :u"), {"u": user_id}
        )
    assert written.scalar_one() == 0


async def test_an_empty_value_still_clears_the_link(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "social-clear@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.patch(url(user_id, "profile"), json={"social_twitter": "ada"}, headers=headers)

    response = await api_client.patch(
        url(user_id, "profile"), json={"social_twitter": ""}, headers=headers
    )

    assert response.status_code == 204
    assert (await stored(db_engine, user_id))["social_twitter"] is None


async def test_a_legacy_value_reads_canonical_or_null_on_me(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "social-legacy@example.com")
    await store_legacy(db_engine, user_id, "ada-lovelace", "https://evil.example/ada", None)

    me = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()

    # A bare handle the old system kept is still a handle: published canonical.
    assert me["profile"]["social_linkedin"] == "https://www.linkedin.com/in/ada-lovelace"
    assert me["profile"]["social_twitter"] is None
    assert me["profile"]["social_youtube"] is None
    # Nothing stored was rewritten — the read converts, it does not migrate.
    assert (await stored(db_engine, user_id))["social_twitter"] == "https://evil.example/ada"
    # The owner still sees the row as stored, to delete it (decision 275).
    assert [(k["platform"], k["url"]) for k in me["links"]] == [("x", "https://evil.example/ada")]


async def test_a_legacy_value_reads_canonical_or_null_on_the_public_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "social-public")
    await store_legacy(
        db_engine,
        mentor,
        "https://linkedin.test/ada",
        "https://twitter.com/ada",
        "https://m.youtube.com/@ada",
    )

    body = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()

    assert body["social_linkedin"] is None
    assert body["social_twitter"] == "https://x.com/ada"
    assert body["social_youtube"] == "https://www.youtube.com/@ada"
    assert body["links"] == [
        {"platform": "x", "url": "https://x.com/ada"},
        {"platform": "youtube", "url": "https://www.youtube.com/@ada"},
    ]


async def test_an_accented_linkedin_link_is_stored_as_linkedin_writes_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Owner, 2026-10-18: a mentor with an accented name could not save their
    real LinkedIn link. The decoded form arrives; the encoded one is stored."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "social-accent@example.com")
    headers = bearer(api_token(auth_id))

    response = await api_client.patch(
        url(user_id, "profile"),
        json={"social_linkedin": "https://www.linkedin.com/in/josé-silva-1a2b3c"},
        headers=headers,
    )

    assert response.status_code == 204, response.text
    expected = "https://www.linkedin.com/in/jos%C3%A9-silva-1a2b3c"
    assert (await stored(db_engine, user_id))["social_linkedin"] == expected
    me = (await api_client.get("/api/v1/me", headers=headers)).json()
    assert me["profile"]["social_linkedin"] == expected

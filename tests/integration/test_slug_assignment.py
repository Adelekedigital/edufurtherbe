"""Every account gets a public handle (owner, 2026-11-14).

A placeholder at first sign-in, replaced once by one made from the first name
saved; never changed after; imported handles never touched.
"""

from __future__ import annotations

import datetime as dt
import re
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.test_api_writes import url
from tests.integration.test_first_sign_in import URL, token_for

from app.infra.db.slugs import backfill_slugs, give_slug, settle_name_slug
from app.infra.db.triggers import timestamps_from_source
from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.anyio]

PLACEHOLDER = re.compile(r"^user-[0-9a-z]{8}$")


async def handle_of(engine: AsyncEngine, user_id: UUID) -> tuple[str | None, bool]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT slug, slug_is_placeholder FROM users WHERE id = :u"), {"u": user_id}
            )
        ).one()
    return row[0], row[1]


async def signed_in(api_client: httpx.AsyncClient, email: str) -> tuple[UUID, dict[str, str]]:
    headers = token_for(uuid4(), email)
    me = (await api_client.get(URL, headers=headers)).json()
    return UUID(me["id"]), headers


async def save_name(
    api_client: httpx.AsyncClient, user_id: UUID, headers: dict[str, str], full_name: str
) -> None:
    response = await api_client.patch(
        url(user_id, "profile"), json={"full_name": full_name}, headers=headers
    )
    assert response.status_code == 204, response.text


async def test_a_new_account_starts_with_a_placeholder_handle(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, _ = await signed_in(api_client, "fresh.slug@example.com")

    slug, placeholder = await handle_of(db_engine, user_id)
    assert slug is not None and PLACEHOLDER.match(slug)
    assert placeholder is True


async def test_the_first_name_saved_replaces_the_placeholder_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await signed_in(api_client, "chloe.slug@example.com")

    await save_name(api_client, user_id, headers, "Chloé Ńwosu")
    assert await handle_of(db_engine, user_id) == ("chloe-nwosu", False)

    await save_name(api_client, user_id, headers, "Chloe Okafor")
    assert await handle_of(db_engine, user_id) == ("chloe-nwosu", False)


async def test_a_name_that_gives_nothing_keeps_the_placeholder(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await signed_in(api_client, "kanji.slug@example.com")
    before, _ = await handle_of(db_engine, user_id)

    await save_name(api_client, user_id, headers, "山田 太郎")

    assert await handle_of(db_engine, user_id) == (before, True)


async def test_an_imported_handle_is_final_even_before_a_name(db_engine: AsyncEngine) -> None:
    """194 imported accounts have no first name; their handle must not move
    when they save one."""
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "imported.slug@example.com", slug="user-legacyab")
    async with AsyncSession(db_engine) as session:
        await session.execute(
            text("UPDATE users SET first_name = 'Ada' WHERE id = :u"), {"u": user_id}
        )
        assert await settle_name_slug(session, user_id) is None
        await session.commit()

    assert await handle_of(db_engine, user_id) == ("user-legacyab", False)


async def test_a_clash_counts_up_and_a_reserved_word_is_suffixed(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        first = await add_user(
            conn, "ada1.slug@example.com", first_name="Ada", last_name="Lovelace"
        )
        second = await add_user(
            conn, "ada2.slug@example.com", first_name="Ada", last_name="Lovelace"
        )
        reserved = await add_user(conn, "admin.slug@example.com", first_name="Admin")
    async with AsyncSession(db_engine) as session:
        for user_id in (first, second, reserved):
            await give_slug(session, user_id)
        await session.commit()

    assert (await handle_of(db_engine, first))[0] == "ada-lovelace"
    assert (await handle_of(db_engine, second))[0] == "ada-lovelace-2"
    assert (await handle_of(db_engine, reserved))[0] == "admin-2"


async def test_the_backfill_fills_only_what_is_missing(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        named = await add_user(conn, "bf1.slug@example.com", first_name="Grace", last_name="Hopper")
        nameless = await add_user(conn, "bf2.slug@example.com")
        kept = await add_user(conn, "bf3.slug@example.com", slug="kept-handle", first_name="Kat")
        odd = await add_user(conn, "bf4.slug@example.com", slug="-odd-handle")
    async with AsyncSession(db_engine) as session:
        result = await backfill_slugs(session)
        await session.commit()

    assert (await handle_of(db_engine, named)) == ("grace-hopper", False)
    slug, placeholder = await handle_of(db_engine, nameless)
    assert slug is not None and PLACEHOLDER.match(slug) and placeholder
    assert await handle_of(db_engine, kept) == ("kept-handle", False)
    assert await handle_of(db_engine, odd) == ("-odd-handle", False)
    assert "-odd-handle" in result.unservable
    assert result.from_name >= 1 and result.placeholders >= 1

    async with AsyncSession(db_engine) as session:
        again = await backfill_slugs(session)
        await session.rollback()
    assert (again.from_name, again.placeholders) == (0, 0)


async def test_a_handle_a_deleted_account_can_restore_is_not_given_away(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.begin() as conn:
        await add_user(
            conn, "gone.slug@example.com", slug="john-smith", deleted_at=dt.datetime.now(dt.UTC)
        )
        newcomer = await add_user(
            conn, "john.slug@example.com", first_name="John", last_name="Smith"
        )
    async with AsyncSession(db_engine) as session:
        await give_slug(session, newcomer)
        await session.commit()

    assert (await handle_of(db_engine, newcomer))[0] == "john-smith-2"


async def test_the_backfill_keeps_a_migrated_rows_modified_date(db_engine: AsyncEngine) -> None:
    legacy = dt.datetime(2024, 3, 1, 12, 0, tzinfo=dt.UTC)
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "dated.slug@example.com", first_name="Dated")
        async with timestamps_from_source(conn, "users"):
            await conn.execute(
                text("UPDATE users SET updated_at = :t WHERE id = :u"), {"t": legacy, "u": user_id}
            )
    async with AsyncSession(db_engine) as session:
        await backfill_slugs(session)
        await session.commit()

    async with db_engine.connect() as conn:
        updated = await conn.scalar(
            text("SELECT updated_at FROM users WHERE id = :u"), {"u": user_id}
        )
    assert (await handle_of(db_engine, user_id))[0] == "dated"
    assert updated == legacy


async def test_a_last_name_save_settles_a_placeholder_once_a_first_name_exists(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user_id, headers = await signed_in(api_client, "lastonly.slug@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET first_name = 'Lara' WHERE id = :u"), {"u": user_id}
        )

    response = await api_client.patch(
        url(user_id, "profile"), json={"last_name": "Croft"}, headers=headers
    )

    assert response.status_code == 204, response.text
    assert await handle_of(db_engine, user_id) == ("lara-croft", False)

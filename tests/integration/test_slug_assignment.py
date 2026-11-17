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
from tests.integration.test_identity_load import record as load_record

from app.domain.transform import transform_users
from app.infra.db.account_purge import purge_account, restore_account
from app.infra.db.slugs import backfill_slugs, give_slug, settle_name_slug
from app.infra.db.triggers import timestamps_from_source
from app.infra.etl.loader import UserLoader
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


async def test_a_first_name_alone_waits_for_the_last_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A handle is final, so it waits for both names (owner, 16 Nov): `ada`
    alone would use up the short handles for common names."""
    user_id, headers = await signed_in(api_client, "first.only@example.com")
    before, _ = await handle_of(db_engine, user_id)

    response = await api_client.patch(
        url(user_id, "profile"), json={"first_name": "Ada"}, headers=headers
    )
    assert response.status_code == 204, response.text
    assert await handle_of(db_engine, user_id) == (before, True)

    response = await api_client.patch(
        url(user_id, "profile"), json={"last_name": "Okafor"}, headers=headers
    )
    assert response.status_code == 204, response.text
    assert await handle_of(db_engine, user_id) == ("ada-okafor", False)


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


async def test_a_clash_counts_up(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        first = await add_user(
            conn, "ada1.slug@example.com", first_name="Ada", last_name="Lovelace"
        )
        second = await add_user(
            conn, "ada2.slug@example.com", first_name="Ada", last_name="Lovelace"
        )
    async with AsyncSession(db_engine) as session:
        for user_id in (first, second):
            await give_slug(session, user_id)
        await session.commit()

    assert (await handle_of(db_engine, first))[0] == "ada-lovelace"
    assert (await handle_of(db_engine, second))[0] == "ada-lovelace-2"


async def test_the_backfill_fills_only_what_is_missing(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        named = await add_user(conn, "bf1.slug@example.com", first_name="Grace", last_name="Hopper")
        nameless = await add_user(conn, "bf2.slug@example.com")
        kept = await add_user(conn, "bf3.slug@example.com", slug="kept-handle", first_name="Kat")
        odd = await add_user(conn, "bf4.slug@example.com", slug="-odd-handle")
    async with AsyncSession(db_engine) as session:
        result = await backfill_slugs(session, apply=True)
        await session.commit()

    assert (await handle_of(db_engine, named)) == ("grace-hopper", False)
    slug, placeholder = await handle_of(db_engine, nameless)
    assert slug is not None and PLACEHOLDER.match(slug) and placeholder
    assert await handle_of(db_engine, kept) == ("kept-handle", False)
    assert await handle_of(db_engine, odd) == ("-odd-handle", False)
    assert "-odd-handle" in result.unservable
    assert result.from_name >= 1 and result.placeholders >= 1

    async with AsyncSession(db_engine) as session:
        again = await backfill_slugs(session, apply=True)
        await session.rollback()
    assert (again.from_name, again.placeholders) == (0, 0)


async def test_a_handle_a_deleted_account_can_restore_is_not_given_away(
    db_engine: AsyncEngine,
) -> None:
    now = dt.datetime.now(dt.UTC)
    async with db_engine.begin() as conn:
        gone = await add_user(conn, "gone.slug@example.com", slug="john-smith", deleted_at=now)
        await conn.execute(
            text("UPDATE users SET deletion_purge_at = :p WHERE id = :u"),
            {"p": now + dt.timedelta(days=30), "u": gone},
        )
        newcomer = await add_user(
            conn, "john.slug@example.com", first_name="John", last_name="Smith"
        )
    async with AsyncSession(db_engine) as session:
        await give_slug(session, newcomer)
        await session.commit()

    assert (await handle_of(db_engine, newcomer))[0] == "john-smith-2"


async def test_a_handle_a_deleted_account_cannot_restore_is_free(db_engine: AsyncEngine) -> None:
    """Deleted with no restore window (an admin removal, an import): it never
    comes back, so its handle does not push a new person to a suffix."""
    async with db_engine.begin() as conn:
        await add_user(
            conn, "removed.slug@example.com", slug="mary-jones", deleted_at=dt.datetime.now(dt.UTC)
        )
        newcomer = await add_user(
            conn, "mary.slug@example.com", first_name="Mary", last_name="Jones"
        )
    async with AsyncSession(db_engine) as session:
        await give_slug(session, newcomer)
        await session.commit()

    assert (await handle_of(db_engine, newcomer))[0] == "mary-jones"


async def test_a_reload_that_brings_a_name_settles_a_placeholder(db_engine: AsyncEngine) -> None:
    rows = transform_users([load_record(Slug=None)]).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        user_id = await conn.scalar(
            text(
                "UPDATE users SET slug = 'user-zzzz9999', slug_is_placeholder = true "
                "WHERE legacy_bubble_id = :b RETURNING id"
            ),
            {"b": rows[0].legacy_bubble_id},
        )
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)

    slug, placeholder = await handle_of(db_engine, user_id)
    assert placeholder is False
    assert slug is not None and not slug.startswith("user-")


async def test_the_backfill_keeps_a_migrated_rows_modified_date(db_engine: AsyncEngine) -> None:
    legacy = dt.datetime(2024, 3, 1, 12, 0, tzinfo=dt.UTC)
    async with db_engine.begin() as conn:
        user_id = await add_user(
            conn, "dated.slug@example.com", first_name="Dated", last_name="Row"
        )
        async with timestamps_from_source(conn, "users"):
            await conn.execute(
                text("UPDATE users SET updated_at = :t WHERE id = :u"), {"t": legacy, "u": user_id}
            )
    async with AsyncSession(db_engine) as session:
        await backfill_slugs(session, apply=True)
        await session.commit()

    async with db_engine.connect() as conn:
        updated = await conn.scalar(
            text("SELECT updated_at FROM users WHERE id = :u"), {"u": user_id}
        )
    assert (await handle_of(db_engine, user_id))[0] == "dated-row"
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


async def test_a_reload_with_a_legacy_slug_makes_it_final(db_engine: AsyncEngine) -> None:
    """A placeholder the backfill gave is replaced by the legacy slug a later
    load brings, and that handle is final: a later name save keeps it."""
    rows = transform_users([load_record(Slug=None)]).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        user_id = await conn.scalar(
            text(
                "UPDATE users SET slug = 'user-abcd1234', slug_is_placeholder = true "
                "WHERE legacy_bubble_id = :b RETURNING id"
            ),
            {"b": rows[0].legacy_bubble_id},
        )

    reloaded = transform_users([load_record(Slug="ada-okafor")]).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(reloaded)
    assert await handle_of(db_engine, user_id) == ("ada-okafor", False)

    without = transform_users([load_record(Slug=None)]).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(without)
    assert await handle_of(db_engine, user_id) == ("ada-okafor", False)


async def test_a_legacy_slug_another_account_holds_is_not_taken(db_engine: AsyncEngine) -> None:
    """A clash with a live account's handle, or one a self-deleted account can
    still restore, gives the imported account its own handle instead of
    aborting the load or blocking the restore."""
    async with db_engine.begin() as conn:
        live_id = await add_user(conn, "holder.live@example.com", slug="ada-okafor")
        gone_id = await add_user(conn, "holder.gone@example.com", slug="bayo-ade")
        await conn.execute(
            text(
                "UPDATE users SET deleted_at = now(), "
                "deletion_purge_at = now() + interval '30 days' WHERE id = :u"
            ),
            {"u": gone_id},
        )
    rows = transform_users(
        [
            load_record(Slug="ada-okafor"),
            load_record(
                Slug="bayo-ade",
                bubble_id="1700000000000x999999999999999999",
                email="bayo.import@example.com",
            ),
        ]
    ).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        handles = dict(
            (
                await conn.execute(
                    text(
                        "SELECT legacy_bubble_id, slug FROM users WHERE legacy_bubble_id = ANY(:b)"
                    ),
                    {"b": [r.legacy_bubble_id for r in rows]},
                )
            ).all()
        )

    assert await handle_of(db_engine, live_id) == ("ada-okafor", False)
    assert set(handles) == {r.legacy_bubble_id for r in rows}
    assert "ada-okafor" not in handles.values() and "bayo-ade" not in handles.values()
    assert all(slug for slug in handles.values())


async def test_a_reload_never_moves_a_final_handle(db_engine: AsyncEngine) -> None:
    """A handle settled in the app (backfill or a name save) is published in
    links; a later load bringing a different legacy slug must not replace it."""
    rows = transform_users([load_record(Slug=None)]).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        user_id = await conn.scalar(
            text(
                "UPDATE users SET slug = 'ada-settled', slug_is_placeholder = false "
                "WHERE legacy_bubble_id = :b RETURNING id"
            ),
            {"b": rows[0].legacy_bubble_id},
        )

    reloaded = transform_users([load_record(Slug="ada-okafor")]).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(reloaded)

    assert await handle_of(db_engine, user_id) == ("ada-settled", False)


async def test_a_dry_run_backfill_writes_nothing(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "dry.slug@example.com", first_name="Dry", last_name="Run")
    async with AsyncSession(db_engine) as session:
        result = await backfill_slugs(session, apply=False)
        await session.commit()

    assert result.from_name >= 1
    assert await handle_of(db_engine, user_id) == (None, False)


async def test_a_last_name_alone_waits_for_a_first_name(db_engine: AsyncEngine) -> None:
    """A handle is named after the person: without a first name it stays a
    placeholder, so a later first name still makes the real one."""
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "lastname.slug@example.com", last_name="Okafor")
    async with AsyncSession(db_engine) as session:
        await give_slug(session, user_id)
        await session.commit()

    slug, placeholder = await handle_of(db_engine, user_id)
    assert slug is not None and PLACEHOLDER.match(slug) and placeholder


async def test_a_loaded_account_without_a_legacy_slug_gets_one_at_load(
    db_engine: AsyncEngine,
) -> None:
    rows = transform_users([load_record(Slug=None)]).rows
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        slug = await conn.scalar(
            text("SELECT slug FROM users WHERE legacy_bubble_id = :b"),
            {"b": rows[0].legacy_bubble_id},
        )

    assert slug is not None


async def test_a_restored_account_without_a_handle_gets_one(db_engine: AsyncEngine) -> None:
    now = dt.datetime.now(dt.UTC)
    async with db_engine.begin() as conn:
        user_id = await add_user(
            conn, "restore.slug@example.com", first_name="Remy", last_name="Lebeau", deleted_at=now
        )
        await conn.execute(
            text("UPDATE users SET deletion_purge_at = :p WHERE id = :u"),
            {"p": now + dt.timedelta(days=30), "u": user_id},
        )
    async with AsyncSession(db_engine) as session:
        assert await restore_account(session, user_id, now=now)
        await session.commit()

    assert await handle_of(db_engine, user_id) == ("remy-lebeau", False)


async def test_a_purged_account_holds_no_handle_and_no_placeholder_flag(
    db_engine: AsyncEngine,
) -> None:
    now = dt.datetime.now(dt.UTC)
    async with db_engine.begin() as conn:
        user_id = await add_user(
            conn, "purge.slug@example.com", deleted_at=now - dt.timedelta(days=31)
        )
        await conn.execute(
            text(
                "UPDATE users SET slug = 'user-purge123', slug_is_placeholder = true, "
                "deletion_purge_at = :p WHERE id = :u"
            ),
            {"p": now - dt.timedelta(minutes=1), "u": user_id},
        )
    async with AsyncSession(db_engine) as session:
        assert await purge_account(session, user_id, now=now) is not None
        await session.commit()

    assert await handle_of(db_engine, user_id) == (None, False)

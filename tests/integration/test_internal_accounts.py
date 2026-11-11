"""`users.is_internal`: kept by the database, read by the analytics exclusions (decision 329).

A live admin grant or a demo address makes an account internal, through every
writer (the provisioning CLI, the loader, a revoke typed by hand), and the
analytics exclusions read the flag and nothing else.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.enums import AdminRole
from app.infra.db.analytics_scope import counted_session, counted_user
from app.infra.db.demo_seed import DEMO_DOMAIN, DEMO_EMAIL_LIKE
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User
from app.infra.db.provisioning_store import ProvisioningStore
from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_user(engine: AsyncEngine, email: str | None = None) -> UUID:
    async with engine.begin() as conn:
        return await add_user(conn, email or f"{uuid4()}@example.test")


async def flag(engine: AsyncEngine, user: UUID) -> tuple[bool, dt.datetime]:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT is_internal, updated_at FROM users WHERE id = :u"), {"u": user}
            )
        ).one()
    return row.is_internal, row.updated_at


async def sql(engine: AsyncEngine, statement: str, **values: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), values)


async def test_an_ordinary_account_is_not_internal(db_engine: AsyncEngine) -> None:
    assert (await flag(db_engine, await a_user(db_engine)))[0] is False


async def test_a_demo_address_is_internal_from_its_insert(db_engine: AsyncEngine) -> None:
    demo = await a_user(db_engine, f"d-{uuid4()}@{DEMO_DOMAIN}")

    assert (await flag(db_engine, demo))[0] is True


async def test_the_databases_demo_pattern_is_demo_seeds(db_engine: AsyncEngine) -> None:
    """`internal_account()` spells the marker in SQL; this pins it to Python's."""
    async with db_engine.begin() as conn:
        body = await conn.scalar(
            text("SELECT prosrc FROM pg_proc WHERE proname = 'internal_account'")
        )

    assert f"LIKE '{DEMO_EMAIL_LIKE}'" in body


async def test_a_grant_marks_and_a_revoke_clears(
    db_engine: AsyncEngine, store: ProvisioningStore
) -> None:
    admin = await a_user(db_engine)
    _, before = await flag(db_engine, admin)

    assert await store.grant_admin(admin, AdminRole.SUPER_ADMIN)
    granted, at_grant = await flag(db_engine, admin)
    # A second role changes nothing, so the row is not touched again.
    assert await store.grant_admin(admin, AdminRole.MENTOR_APPROVAL)
    _, at_second = await flag(db_engine, admin)
    await sql(
        db_engine,
        "UPDATE admin_users SET revoked_at = now() "
        "WHERE user_id = :u AND admin_role = 'super_admin'",
        u=admin,
    )
    still, _ = await flag(db_engine, admin)
    await sql(db_engine, "UPDATE admin_users SET revoked_at = now() WHERE user_id = :u", u=admin)
    revoked, _ = await flag(db_engine, admin)

    assert granted is True
    assert at_grant == before, "the CLI holds the timestamp trigger off"
    assert at_second == at_grant
    assert still is True, "one live grant is left"
    assert revoked is False


async def test_a_revoke_leaves_a_demo_account_internal(
    db_engine: AsyncEngine, store: ProvisioningStore
) -> None:
    demo = await a_user(db_engine, f"d-{uuid4()}@{DEMO_DOMAIN}")
    await store.grant_admin(demo, AdminRole.SUPER_ADMIN)

    await sql(db_engine, "UPDATE admin_users SET revoked_at = now() WHERE user_id = :u", u=demo)
    await sql(db_engine, "DELETE FROM admin_users WHERE user_id = :u", u=demo)

    assert (await flag(db_engine, demo))[0] is True


async def test_the_flag_cannot_be_written_by_hand(db_engine: AsyncEngine) -> None:
    user = await a_user(db_engine)

    await sql(db_engine, "UPDATE users SET is_internal = true WHERE id = :u", u=user)

    assert (await flag(db_engine, user))[0] is False


async def test_the_analytics_exclusions_read_the_flag(db_engine: AsyncEngine) -> None:
    counted = await a_user(db_engine)
    admin = await a_user(db_engine)
    demo = await a_user(db_engine, f"d-{uuid4()}@{DEMO_DOMAIN}")
    await sql(
        db_engine,
        "INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'limited_access')",
        u=admin,
    )
    revoked = await a_user(db_engine)
    await sql(
        db_engine,
        "INSERT INTO admin_users (user_id, admin_role, revoked_at) "
        "VALUES (:u, 'super_admin', now())",
        u=revoked,
    )
    people = (counted, admin, demo, revoked)

    async with AsyncSession(db_engine) as session:
        kept = set(
            await session.scalars(select(User.id).where(User.id.in_(people), counted_user(User.id)))
        )
        flagged = set(
            await session.scalars(select(User.id).where(User.id.in_(people), User.is_internal))
        )

    assert kept == {counted, revoked}
    assert flagged == {admin, demo}


async def test_a_session_with_an_internal_party_is_not_counted(db_engine: AsyncEngine) -> None:
    mentor, mentee, demo = (
        await a_user(db_engine),
        await a_user(db_engine),
        await a_user(db_engine, f"d-{uuid4()}@{DEMO_DOMAIN}"),
    )
    ids = []
    for party in (mentee, demo):
        async with db_engine.begin() as conn:
            ids.append(
                await conn.scalar(
                    text(
                        "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, "
                        "status) VALUES (:m, :e, now(), 30, 'cancelled') RETURNING id"
                    ),
                    {"m": mentor, "e": party},
                )
            )

    async with AsyncSession(db_engine) as session:
        kept = set(
            await session.scalars(select(Session.id).where(Session.id.in_(ids), counted_session()))
        )

    assert kept == {ids[0]}


@pytest.mark.isolated_db
async def test_a_cli_grant_keeps_a_migrated_admins_modified_date(
    db_engine: AsyncEngine, store: ProvisioningStore
) -> None:
    modified = dt.datetime(2025, 11, 6, 11, 52, tzinfo=dt.UTC)
    async with db_engine.begin() as conn:
        admin = await add_user(conn, f"{uuid4()}@example.test", legacy_bubble_id=f"b-{uuid4()}")
    async with db_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE users DISABLE TRIGGER trg_set_updated_at"))
        await conn.execute(
            text("UPDATE users SET updated_at = :m WHERE id = :u"), {"m": modified, "u": admin}
        )
        await conn.execute(text("ALTER TABLE users ENABLE TRIGGER trg_set_updated_at"))

    await store.grant_admin(admin, AdminRole.SUPER_ADMIN)

    assert await flag(db_engine, admin) == (True, modified)

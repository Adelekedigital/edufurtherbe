"""Who a "it has shipped" send may contact, read from `feature_interest` (#374).

**The case that matters is the closed account.** A row is written while the
account is live, and nothing on the write can stop that account closing next
month: `users.deleted_at` leaves the row in place. So the one recipient read
must drop it in the same statement, and this is the test that stops the rule
being re-reasoned when the first send is built.

Each test waits for its own feature key, because the test database is shared
and another test's `payments` row would otherwise count here.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_mentor_status_log import make_user

from app.infra.db.interest_store import waiting_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def a_feature() -> str:
    return f"ships_{uuid4().hex[:12]}"


async def a_person(engine: AsyncEngine, tag: str) -> UUID:
    return await make_user(engine, uuid4(), f"{tag}-{uuid4().hex[:6]}@example.com")


async def run(engine: AsyncEngine, sql: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def register(engine: AsyncEngine, user_id: UUID, feature: str) -> None:
    await run(
        engine,
        "INSERT INTO feature_interest (user_id, feature) VALUES (:u, :f)",
        u=user_id,
        f=feature,
    )


async def recipients(engine: AsyncEngine, feature: str, limit: int = 100) -> list[UUID]:
    async with async_sessionmaker(engine)() as session:
        return await waiting_for(session, feature, limit=limit)


async def test_a_closed_account_is_not_contacted(db_engine: AsyncEngine) -> None:
    """**The #374 rule**, with its positive half: the live account beside it is
    selected, so a query that returns nobody cannot pass."""
    feature = a_feature()
    stays = await a_person(db_engine, "stays")
    leaves = await a_person(db_engine, "leaves")
    await register(db_engine, stays, feature)
    await register(db_engine, leaves, feature)

    await run(db_engine, "UPDATE users SET deleted_at = now() WHERE id = :u", u=leaves)

    assert await recipients(db_engine, feature) == [stays]


async def test_somebody_already_told_is_not_told_again(db_engine: AsyncEngine) -> None:
    """`notified_at` is why the column exists: a re-run of a send skips them."""
    feature = a_feature()
    told = await a_person(db_engine, "told")
    waiting = await a_person(db_engine, "waiting")
    await register(db_engine, told, feature)
    await register(db_engine, waiting, feature)

    await run(
        db_engine,
        "UPDATE feature_interest SET notified_at = now() WHERE user_id = :u AND feature = :f",
        u=told,
        f=feature,
    )

    assert await recipients(db_engine, feature) == [waiting]


async def test_only_this_feature_s_waiters_oldest_first_in_batches(
    db_engine: AsyncEngine,
) -> None:
    """Scoped to the key, in the order people asked, and bounded by `limit` so a
    send can stamp `notified_at` batch by batch rather than load everyone."""
    feature, other = a_feature(), a_feature()
    first = await a_person(db_engine, "first")
    second = await a_person(db_engine, "second")
    third = await a_person(db_engine, "third")
    elsewhere = await a_person(db_engine, "elsewhere")
    for person in (first, second, third):
        await register(db_engine, person, feature)
    await register(db_engine, elsewhere, other)

    assert await recipients(db_engine, feature) == [first, second, third]
    assert await recipients(db_engine, feature, limit=2) == [first, second]

"""The monthly grant: who gets three credits on the 1st, and who does not.

**The case this file exists for is the second run.** The job is scheduled, and a
scheduled job runs twice — a retry, a manual trigger beside the cron, an
operator checking it works. Granting twice would hand every unlocked mentee six
credits, and nothing downstream would notice: the balance is a `SUM` and it
would simply be right about the wrong number.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.domain.credits import credit_ladder
from app.infra.db.credit_grants import grant_monthly_credits
from app.infra.db.engine import create_session_factory

#: Built from explicit settings, so this suite is about the code rather than
#: about the environment the machine happens to hold.
LADDER = credit_ladder(Settings(_env_file=None))


pytestmark = [pytest.mark.db, pytest.mark.asyncio]

SEPTEMBER = dt.datetime(2026, 9, 1, 6, 0, tzinfo=dt.UTC)
OCTOBER = dt.datetime(2026, 10, 1, 6, 0, tzinfo=dt.UTC)


async def a_user(
    engine: AsyncEngine,
    *,
    role: str = "mentee",
    goal: bool = True,
    unlocked: bool = True,
    deleted: bool = False,
) -> UUID:
    tag = uuid4()
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone, deleted_at) "
                    "VALUES (:e, :r, 'Africa/Lagos', :d) RETURNING id"
                ),
                {
                    "e": f"{tag}@example.com",
                    "r": role,
                    "d": dt.datetime(2026, 1, 1, tzinfo=dt.UTC) if deleted else None,
                },
            )
        ).scalar_one()
        if goal:
            await conn.execute(
                text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user_id}
            )
        if unlocked:
            await conn.execute(
                text(
                    "INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id) "
                    "VALUES (:u, NULL)"
                ),
                {"u": user_id},
            )
        return UUID(str(user_id))


async def balance_of(engine: AsyncEngine, user_id: UUID) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT COALESCE(SUM(quantity_remaining), 0) "
                        "FROM credit_lots WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
        )


async def ledger_of(engine: AsyncEngine, user_id: UUID) -> list[tuple[int, str]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT delta, reason FROM credit_transactions "
                "WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user_id},
        )
        return [(row[0], row[1]) for row in rows]


async def run_grant(engine: AsyncEngine, *, now: dt.datetime) -> int:
    factory = create_session_factory(engine)
    async with factory() as db:
        granted = await grant_monthly_credits(db, now=now, ladder=LADDER)
        await db.commit()
        return granted


# --------------------------------------------------------------------------
# Who gets it
# --------------------------------------------------------------------------


async def test_an_unlocked_mentee_is_granted_three(db_engine: AsyncEngine) -> None:
    user = await a_user(db_engine)

    granted = await run_grant(db_engine, now=SEPTEMBER)

    assert granted == 1
    assert await balance_of(db_engine, user) == 3
    assert await ledger_of(db_engine, user) == [(3, "grant")]


async def test_a_mentor_with_a_mentee_goal_is_granted_too(db_engine: AsyncEngine) -> None:
    """**The predicate is having a mentee goal, not "is not a mentor".**

    Authorization here is profile existence, so a dual-role user is both — and a
    negative predicate would silently stop somebody booking who can book.
    """
    user = await a_user(db_engine, role="mentor")

    await run_grant(db_engine, now=SEPTEMBER)

    assert await balance_of(db_engine, user) == 3


async def test_a_user_with_no_mentee_goal_gets_nothing(db_engine: AsyncEngine) -> None:
    """Credits buy sessions, and somebody who is not a mentee books none."""
    user = await a_user(db_engine, role="mentor", goal=False)

    granted = await run_grant(db_engine, now=SEPTEMBER)

    assert granted == 0
    assert await balance_of(db_engine, user) == 0


async def test_a_locked_mentee_gets_nothing(db_engine: AsyncEngine) -> None:
    """**The gate a qualifying invite opens.** Without this the referral
    programme rewards nothing: the monthly grant would arrive whether or not
    anybody ever invited a soul."""
    user = await a_user(db_engine, unlocked=False)

    granted = await run_grant(db_engine, now=SEPTEMBER)

    assert granted == 0
    assert await balance_of(db_engine, user) == 0


async def test_a_deleted_user_gets_nothing(db_engine: AsyncEngine) -> None:
    """Their rows survive because the ledger is evidence; the grant does not
    follow them out."""
    user = await a_user(db_engine, deleted=True)

    granted = await run_grant(db_engine, now=SEPTEMBER)

    assert granted == 0
    assert await balance_of(db_engine, user) == 0


# --------------------------------------------------------------------------
# Running twice — the case the index exists for
# --------------------------------------------------------------------------


async def test_a_second_run_in_the_same_month_grants_nothing(db_engine: AsyncEngine) -> None:
    """**A scheduled job runs twice.** A retry, a manual trigger beside the
    cron, an operator checking it works — and granting again would hand every
    unlocked mentee six credits, with nothing downstream noticing because the
    balance is a `SUM` that would simply be right about the wrong number.
    """
    user = await a_user(db_engine)

    first = await run_grant(db_engine, now=SEPTEMBER)
    second = await run_grant(db_engine, now=SEPTEMBER)

    assert (first, second) == (1, 0)
    assert await balance_of(db_engine, user) == 3
    assert await ledger_of(db_engine, user) == [(3, "grant")]


async def test_a_late_run_still_counts_as_that_month(db_engine: AsyncEngine) -> None:
    """The guard keys on the expiry, which is the month's end rather than the
    day the job happened to run — so a job that fired on the 1st and again on
    the 3rd does not pay twice."""
    user = await a_user(db_engine)
    await run_grant(db_engine, now=SEPTEMBER)

    granted = await run_grant(db_engine, now=dt.datetime(2026, 9, 3, 6, 0, tzinfo=dt.UTC))

    assert granted == 0
    assert await balance_of(db_engine, user) == 3


async def test_the_next_month_grants_again(db_engine: AsyncEngine) -> None:
    """**The accepting half, and the point of the whole job.** A guard keyed on
    the user alone would stop the recurring grant recurring, and every rejecting
    test above would still pass."""
    user = await a_user(db_engine)
    await run_grant(db_engine, now=SEPTEMBER)

    granted = await run_grant(db_engine, now=OCTOBER)

    assert granted == 1
    assert await balance_of(db_engine, user) == 6


# --------------------------------------------------------------------------
# What the lot looks like
# --------------------------------------------------------------------------


async def test_the_grant_expires_at_the_next_reset(db_engine: AsyncEngine) -> None:
    """Perishable, unlike the starter. The card's "Next reset date" is this
    instant, and it is exclusive — a September lot survives all of 30 September
    and dies as October opens."""
    user = await a_user(db_engine)

    await run_grant(db_engine, now=SEPTEMBER)

    async with db_engine.begin() as conn:
        expires = (
            await conn.execute(
                text("SELECT expires_at FROM credit_lots WHERE user_id = :u"), {"u": user}
            )
        ).scalar_one()

    assert expires == OCTOBER.replace(hour=0)


async def test_every_grant_writes_its_ledger_row(db_engine: AsyncEngine) -> None:
    """A balance that rose with nothing saying why is the state D8 chose a
    ledger over a counter to prevent — and a batch job is exactly where a
    missing pairing would go unnoticed."""
    users = [await a_user(db_engine) for _ in range(3)]

    await run_grant(db_engine, now=SEPTEMBER)

    for user in users:
        assert await ledger_of(db_engine, user) == [(3, "grant")]

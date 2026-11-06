"""The monthly grant: who gets three credits on the 1st, and who does not.

**The case this file exists for is the second run.** The job is scheduled, and a
scheduled job runs twice — a retry, a manual trigger beside the cron, an
operator checking it works. Granting twice would hand every unlocked mentee six
credits, and nothing downstream would notice: the balance is a `SUM` and it
would simply be right about the wrong number.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import until_blocked

from app.core.config import Settings
from app.domain.credits import credit_ladder
from app.domain.enums import CreditSource
from app.infra.db.credit_grants import grant_monthly_credits, top_up_levels
from app.infra.db.credit_writer import grant
from app.infra.db.engine import create_session_factory
from conftest import add_user

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
    onboarded: bool = False,
    invited: bool = False,
) -> UUID:
    """A user holding exactly the facts named (decision 308's level reads them)."""
    tag = uuid4()
    async with engine.begin() as conn:
        user_id = await add_user(
            conn,
            f"{tag}@example.com",
            primary_role=role,
            timezone="Africa/Lagos",
            deleted_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC) if deleted else None,
        )
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
        if onboarded:
            await conn.execute(
                text("INSERT INTO user_onboarding (user_id, completed_at) VALUES (:u, now())"),
                {"u": user_id},
            )
        if invited:
            await conn.execute(
                text(
                    "INSERT INTO referrals (referrer_id, code, signed_up_at, qualified_at) "
                    "VALUES (:u, :c, now(), now())"
                ),
                {"u": user_id, "c": uuid4().hex},
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


# --------------------------------------------------------------------------
# Credit levels (decision 308)
# --------------------------------------------------------------------------


async def test_the_first_pays_every_mentee_their_level(db_engine: AsyncEngine) -> None:
    """Levels 0, 1, 2 and 3 are paid 0, 1, 2 and 3, each with a ledger row and
    a renewal email carrying what it paid."""
    nobody = await a_user(db_engine, unlocked=False)
    onboarded = await a_user(db_engine, unlocked=False, onboarded=True)
    invited = await a_user(db_engine, unlocked=False, onboarded=True, invited=True)
    unlocked = await a_user(db_engine, onboarded=True, invited=True)

    granted = await run_grant(db_engine, now=SEPTEMBER)

    assert granted == 3
    expected = {nobody: 0, onboarded: 1, invited: 2, unlocked: 3}
    for user, level in expected.items():
        assert await balance_of(db_engine, user) == level
    assert await ledger_of(db_engine, invited) == [(2, "grant")]
    assert await renewal_counts(db_engine, [nobody, onboarded, invited, unlocked]) == {
        onboarded: "1",
        invited: "2",
        unlocked: "3",
    }


async def test_an_invite_before_onboarding_pays_nothing(db_engine: AsyncEngine) -> None:
    """Level 2 is level 1 plus an invite: an invite alone is level 0."""
    user = await a_user(db_engine, unlocked=False, invited=True)

    assert await run_grant(db_engine, now=SEPTEMBER) == 0
    assert await balance_of(db_engine, user) == 0


async def test_a_rerun_after_spending_pays_nothing(db_engine: AsyncEngine) -> None:
    """**The allowance rule.** Owed is counted from what the period *granted*,
    not from what is held — so spending the month and re-running the job does
    not refill it."""
    user = await a_user(db_engine)
    await run_grant(db_engine, now=SEPTEMBER)
    await spend_all(db_engine, user)

    second = await run_grant(db_engine, now=SEPTEMBER)

    assert second == 0
    assert await balance_of(db_engine, user) == 0


async def test_a_period_already_topped_up_pays_only_what_is_left(db_engine: AsyncEngine) -> None:
    """A level-1 credit paid mid-month by a top-up counts: the 1st's run in
    that period pays the rest of the level as a `level_top_up`, never a second
    `monthly_free` — so the one-per-period index is never what refuses it."""
    user = await a_user(db_engine)
    async with create_session_factory(db_engine)() as db:
        await grant(db, user, source=CreditSource.LEVEL_TOP_UP, quantity=1, now=SEPTEMBER)
        await db.commit()

    await run_grant(db_engine, now=SEPTEMBER)

    assert await balance_of(db_engine, user) == 3
    assert await sources_of(db_engine, user) == ["level_top_up", "monthly_free"]


async def test_a_rerun_after_a_rise_with_no_event_pays_the_rest_as_a_top_up(
    db_engine: AsyncEngine,
) -> None:
    """**The catch-up the deploy relies on.** A level can rise with no top-up
    event (a fact written directly, or existing mentees on deploy day); running
    the job again pays the rest. The period already holds the 1st's
    `monthly_free` lot, so the rest is a `level_top_up` — a second
    `monthly_free` would hit the one-per-period index and fail the whole run."""
    user = await a_user(db_engine, unlocked=False, onboarded=True)
    await run_grant(db_engine, now=SEPTEMBER)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO referrals (referrer_id, code, signed_up_at, qualified_at) "
                "VALUES (:u, :c, now(), now())"
            ),
            {"u": user, "c": uuid4().hex},
        )

    assert await run_grant(db_engine, now=SEPTEMBER) == 1

    assert await balance_of(db_engine, user) == 2
    assert await sources_of(db_engine, user) == ["monthly_free", "level_top_up"]


async def test_refunds_and_support_grants_are_outside_the_level(db_engine: AsyncEngine) -> None:
    """Both may lift a balance above the level (owner, 2026-11-05): neither
    shrinks what the month pays."""
    user = await a_user(db_engine)
    async with create_session_factory(db_engine)() as db:
        await grant(db, user, source=CreditSource.ADMIN_GRANT, quantity=2, now=SEPTEMBER)
        await db.commit()

    await run_grant(db_engine, now=SEPTEMBER)

    assert await balance_of(db_engine, user) == 2 + 3


async def test_the_first_and_a_top_up_racing_pay_the_level_once(db_engine: AsyncEngine) -> None:
    """**The race the users' row lock exists for.** A top-up holding the lock
    has paid level 1 but not committed; the 1st's run, unlocked, would read
    nothing granted and pay all three. Locked, it waits, sees the top-up, and
    pays only the rest."""
    user = await a_user(db_engine)
    async with create_session_factory(db_engine)() as topping:
        await top_up_levels(topping, [user], ladder=LADDER, now=SEPTEMBER)
        job = asyncio.create_task(run_grant(db_engine, now=SEPTEMBER))
        await until_blocked(db_engine)
        await topping.commit()
    await job

    assert await balance_of(db_engine, user) == 3


async def spend_all(engine: AsyncEngine, user_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE credit_lots SET quantity_remaining = 0 WHERE user_id = :u"),
            {"u": user_id},
        )


async def sources_of(engine: AsyncEngine, user_id: UUID) -> list[str]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT source FROM credit_lots WHERE user_id = :u ORDER BY created_at, source"),
            {"u": user_id},
        )
        return [row[0] for row in rows]


async def renewal_counts(engine: AsyncEngine, user_ids: list[UUID]) -> dict[UUID, str]:
    """Who was sent a renewal, and the `credit_count` it carries."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT entity_id, payload ->> 'credit_count' FROM outbox_events "
                "WHERE event_type = 'credits_renewed' AND entity_id = ANY(:ids)"
            ),
            {"ids": user_ids},
        )
        return {UUID(str(row[0])): row[1] for row in rows}

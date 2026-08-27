"""Loading opening balances, twice, and the ledger that has to explain them.

**Twice is the whole point.** The runbook rehearses before the freeze and loads
again at cutover, so a second run is the normal path rather than an edge case —
and without the two partial unique indexes every migrated user's balance would
simply double, with the balance being a `SUM` that would be right about the
wrong number.

**The second subject is the ledger.** A lot without a `grant` row is a balance
that rose with nothing saying why, which is the state D8 chose a ledger over a
counter to prevent, and it is invisible to a row-count check.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.transform.credits import ACTIVE_WITHIN, plan_opening_balances
from app.infra.etl.credits import CreditLoader, finished_onboarding, recently_seen
from app.infra.etl.reconcile import reconcile_credits

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CUTOVER = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
#: When a migrated user registered in Bubble — long before any window.
ANCIENT = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
EXPIRY = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def record(anchor: str, credit: str) -> dict[str, Any]:
    return {"unique id": anchor, "bookingCredit": credit}


async def a_migrated_user(
    engine: AsyncEngine, anchor: str, *, finished: bool = True, active: bool | None = True
) -> None:
    """A user as `load_identity` would leave them, with their onboarding row.

    Written straight to the tables rather than through the identity loader: this
    file is about the credit loader, and running a second ETL to arrange a
    fixture makes a failure here ambiguous between the two.
    """
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, legacy_bubble_id, primary_role, timezone, "
                    "last_active_at, created_at) "
                    "VALUES (:e, :a, 'mentee', 'Africa/Lagos', :la, :ca) RETURNING id"
                ),
                {
                    "e": f"{anchor}@example.com",
                    "a": anchor,
                    # True: active inside the window. False: long dormant.
                    # None: never active at all — the signup-only case.
                    "la": None
                    if active is None
                    else (
                        (CUTOVER - dt.timedelta(days=30))
                        if active
                        else (CUTOVER - ACTIVE_WITHIN - dt.timedelta(days=30))
                    ),
                    # **Written explicitly, and the dormant case is why.**
                    # `created_at` defaults to `now()`, which is inside every
                    # window — so a fixture that leaves it alone makes each
                    # user look like a fresh signup and the second half of the
                    # disjunction rescues everybody. A migrated user registered
                    # in Bubble years ago; only the newcomer is recent.
                    "ca": ANCIENT if active is not None else CUTOVER - dt.timedelta(days=21),
                },
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO user_onboarding (user_id, last_step, completed_at) "
                "VALUES (:u, '6', :c)"
            ),
            {"u": user_id, "c": dt.datetime(2025, 6, 1, tzinfo=dt.UTC) if finished else None},
        )


async def lots_of(engine: AsyncEngine, anchor: str) -> list[tuple[str, int, int, Any]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT l.source, l.quantity_granted, l.quantity_remaining, l.expires_at "
                "FROM credit_lots l JOIN users u ON u.id = l.user_id "
                "WHERE u.legacy_bubble_id = :a ORDER BY l.source"
            ),
            {"a": anchor},
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]


async def ledger_of(engine: AsyncEngine, anchor: str) -> list[tuple[int, str]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT t.delta, t.reason FROM credit_transactions t "
                "JOIN users u ON u.id = t.user_id "
                "WHERE u.legacy_bubble_id = :a ORDER BY t.created_at, t.id"
            ),
            {"a": anchor},
        )
        return [(r[0], r[1]) for r in rows]


async def run_load(engine: AsyncEngine, records: list[dict[str, Any]]) -> Any:
    """Plan and load in one transaction, returning the reconciliation."""
    async with engine.begin() as conn:
        finished = await finished_onboarding(conn)
        seen = await recently_seen(conn, since=CUTOVER - ACTIVE_WITHIN)
        plan = plan_opening_balances(records, cutover=CUTOVER, finished=finished, seen=seen)
        await CreditLoader(conn).load(plan)
        return await reconcile_credits(conn, plan)


# --------------------------------------------------------------------------
# What one run writes
# --------------------------------------------------------------------------


async def test_a_balance_becomes_a_lot_and_the_row_that_explains_it(
    db_engine: AsyncEngine,
) -> None:
    await a_migrated_user(db_engine, "holder")

    result = await run_load(db_engine, [record("holder", "5")])

    assert await lots_of(db_engine, "holder") == [("opening_balance", 5, 5, EXPIRY)]
    assert await ledger_of(db_engine, "holder") == [(5, "grant")]
    assert result.ok


async def test_a_user_who_never_entered_credits_gets_the_starter(
    db_engine: AsyncEngine,
) -> None:
    """`profile_completed`, quantity one, **never expiring** — not an
    `opening_balance` lot of 1, which would claim Bubble held a balance it did
    not and put the reconciliation's two sums permanently out by one."""
    await a_migrated_user(db_engine, "never")

    result = await run_load(db_engine, [record("never", "")])

    assert await lots_of(db_engine, "never") == [("profile_completed", 1, 1, None)]
    assert await ledger_of(db_engine, "never") == [(1, "grant")]
    assert result.legacy_credit_total == 0
    assert result.ok


async def test_a_spent_down_user_gets_nothing(db_engine: AsyncEngine) -> None:
    await a_migrated_user(db_engine, "spent")

    result = await run_load(db_engine, [record("spent", "0")])

    assert await lots_of(db_engine, "spent") == []
    assert await ledger_of(db_engine, "spent") == []
    assert result.ok


async def test_an_unfinished_user_gets_nothing_yet(db_engine: AsyncEngine) -> None:
    """Their onboarding row has no `completed_at`, so `finished_onboarding` does
    not name them and the starter waits for the ordinary producer."""
    await a_migrated_user(db_engine, "halfway", finished=False)

    result = await run_load(db_engine, [record("halfway", "")])

    assert await lots_of(db_engine, "halfway") == []
    assert result.ok


# --------------------------------------------------------------------------
# The second run — the case the runbook makes normal
# --------------------------------------------------------------------------


async def test_a_second_run_produces_identical_rows(db_engine: AsyncEngine) -> None:
    """**The Definition of Done's word, asserted literally.**

    Not "does not raise" and not "the count is the same": the rows themselves,
    before and after. A loader that deleted and re-inserted would satisfy a count
    check and hand every migrated user a new lot id, breaking every ledger row
    pointing at the old one.
    """
    await a_migrated_user(db_engine, "holder")
    await a_migrated_user(db_engine, "never")
    records = [record("holder", "5"), record("never", "")]
    await run_load(db_engine, records)
    before = (await lots_of(db_engine, "holder"), await ledger_of(db_engine, "holder"))

    result = await run_load(db_engine, records)

    assert (await lots_of(db_engine, "holder"), await ledger_of(db_engine, "holder")) == before
    assert await lots_of(db_engine, "never") == [("profile_completed", 1, 1, None)]
    assert result.ok


async def test_a_second_run_does_not_double_the_balance(db_engine: AsyncEngine) -> None:
    """The failure the partial index exists to make impossible, asserted on the
    number a user would see rather than on a row count."""
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])

    await run_load(db_engine, [record("holder", "5")])

    async with db_engine.begin() as conn:
        balance = (
            await conn.execute(
                text(
                    "SELECT SUM(l.quantity_remaining) FROM credit_lots l "
                    "JOIN users u ON u.id = l.user_id WHERE u.legacy_bubble_id = 'holder'"
                )
            )
        ).scalar_one()

    assert balance == 5


async def test_a_rehearsal_that_died_before_the_ledger_is_repaired(
    db_engine: AsyncEngine,
) -> None:
    """**Why the ledger pass keys on absence rather than on `RETURNING`.**

    A run that inserted lots and died before writing the ledger leaves balances
    nothing explains. A `RETURNING`-driven second run inserts no lots — the
    conflict clause skips them — so it would have nothing to write rows for, and
    those balances would stay unexplained forever.
    """
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM credit_transactions t USING users u "
                "WHERE t.user_id = u.id AND u.legacy_bubble_id = 'holder'"
            )
        )
    assert await ledger_of(db_engine, "holder") == []

    result = await run_load(db_engine, [record("holder", "5")])

    assert await ledger_of(db_engine, "holder") == [(5, "grant")]
    assert result.unexplained == ()
    assert result.ok


async def test_a_lot_swept_before_its_grant_was_written_is_still_repaired(
    db_engine: AsyncEngine,
) -> None:
    """**Why the ledger pass narrows on `reason = 'grant'`.**

    The discriminating state is a lot that has *some* movement but not its
    grant: a rehearsal died between the two writes, the month turned, and the
    expiry sweep wrote `lot_expired` against a lot nothing had ever explained.
    Keyed on `NOT EXISTS (any transaction)` the re-run reads that as accounted
    for and walks past, leaving the balance permanently unexplained.

    An earlier version of this test added the expiry to a lot that already had
    its grant, where both spellings behave identically — it passed against the
    broken narrowing and proved nothing.
    """
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])
    async with db_engine.begin() as conn:
        # Erase the grant, then sweep — the interrupted-rehearsal state.
        await conn.execute(
            text(
                "DELETE FROM credit_transactions t USING users u "
                "WHERE t.user_id = u.id AND u.legacy_bubble_id = 'holder'"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO credit_transactions (user_id, credit_lot_id, delta, reason) "
                "SELECT l.user_id, l.id, -5, 'lot_expired' FROM credit_lots l "
                "JOIN users u ON u.id = l.user_id WHERE u.legacy_bubble_id = 'holder'"
            )
        )
        await conn.execute(
            text(
                "UPDATE credit_lots l SET quantity_remaining = 0 FROM users u "
                "WHERE u.id = l.user_id AND u.legacy_bubble_id = 'holder'"
            )
        )

    result = await run_load(db_engine, [record("holder", "5")])

    assert await ledger_of(db_engine, "holder") == [(-5, "lot_expired"), (5, "grant")]
    assert result.unexplained == ()


async def test_a_lot_that_already_has_its_grant_gets_no_second_one(
    db_engine: AsyncEngine,
) -> None:
    """The accepting half. The narrowing must not overcorrect into writing a
    grant on every run — which would inflate the ledger without moving a
    balance, so no `SUM` anywhere would notice."""
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])

    await run_load(db_engine, [record("holder", "5")])
    await run_load(db_engine, [record("holder", "5")])

    assert await ledger_of(db_engine, "holder") == [(5, "grant")]


# --------------------------------------------------------------------------
# Reconciliation — the identity a count check cannot see
# --------------------------------------------------------------------------


async def test_the_credit_totals_are_compared_not_just_the_row_counts(
    db_engine: AsyncEngine,
) -> None:
    """**The Definition of Done's second identity.**

    Three users, three lots, and a count check passes whatever the quantities
    are. Only the sum catches a transform that loaded the right people at the
    wrong balances.
    """
    for anchor in ("a", "b", "c"):
        await a_migrated_user(db_engine, anchor)

    result = await run_load(db_engine, [record("a", "5"), record("b", "2"), record("c", "1")])

    assert result.legacy_credit_total == 8
    assert result.loaded_credit_total == 8
    assert result.totals_agree
    assert result.ok


async def test_the_totals_catch_what_the_counts_cannot(db_engine: AsyncEngine) -> None:
    """**The case the sum exists for, and the only one that separates it.**

    One user, one lot, at the wrong quantity: every count agrees, no anchor is
    missing, nothing is unexplained, and `checks` is entirely green. Only the
    credit total sees it — which is why `ok` has to require `totals_agree` and
    not merely all the checks.
    """
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE credit_lots l SET quantity_granted = 1, quantity_remaining = 1 "
                "FROM users u WHERE u.id = l.user_id AND u.legacy_bubble_id = 'holder'"
            )
        )

    async with db_engine.begin() as conn:
        finished = await finished_onboarding(conn)
        plan = plan_opening_balances([record("holder", "5")], cutover=CUTOVER, finished=finished)
        result = await reconcile_credits(conn, plan)

    assert all(check.ok for check in result.checks)
    assert result.unexplained == ()
    assert result.unaccounted == ()
    assert (result.legacy_credit_total, result.loaded_credit_total) == (5, 1)
    assert not result.totals_agree
    assert not result.ok


async def test_a_spent_credit_does_not_break_the_reconciliation(
    db_engine: AsyncEngine,
) -> None:
    """**The accepting half, and why the sum reads `quantity_granted`.**

    A rehearsal loads, somebody books a session, and the reconciliation runs
    again. The balance has legitimately fallen; the *grant* has not. Read against
    `quantity_remaining` this would report a correctly migrated database as
    broken, every time, from the first booking onward.
    """
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE credit_lots l SET quantity_remaining = 3 "
                "FROM users u WHERE u.id = l.user_id AND u.legacy_bubble_id = 'holder'"
            )
        )

    result = await run_load(db_engine, [record("holder", "5")])

    assert (result.legacy_credit_total, result.loaded_credit_total) == (5, 5)
    assert result.ok


async def test_an_anchor_with_no_user_is_reported_missing_rather_than_raising(
    db_engine: AsyncEngine,
) -> None:
    """`load_identity` has not run, or the user was quarantined by it. The join
    writes nothing and the reconciliation names the anchor — which is what an
    operator needs, where an exception would only say the load failed."""
    await a_migrated_user(db_engine, "present")

    result = await run_load(db_engine, [record("present", "5"), record("orphan", "3")])

    assert not result.ok
    assert result.checks[0].missing == ("orphan",)
    assert result.legacy_credit_total == 8
    assert result.loaded_credit_total == 5


async def test_a_lot_with_no_grant_row_is_reported_unexplained(
    db_engine: AsyncEngine,
) -> None:
    """The check that makes "the ledger explains every balance" a property of the
    run rather than a hope. Asserted by breaking it directly, because a healthy
    load can never produce this state."""
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM credit_transactions t USING users u "
                "WHERE t.user_id = u.id AND u.legacy_bubble_id = 'holder'"
            )
        )

    async with db_engine.begin() as conn:
        finished = await finished_onboarding(conn)
        plan = plan_opening_balances([record("holder", "5")], cutover=CUTOVER, finished=finished)
        result = await reconcile_credits(conn, plan)

    assert result.unexplained == ("holder",)
    assert not result.ok


async def test_a_starter_granted_outside_this_phase_is_not_a_surplus(
    db_engine: AsyncEngine,
) -> None:
    """Somebody who signed up after the extract and completed onboarding through
    the API holds a correct `profile_completed` lot this load did not write.
    Counting every one of them would report it as a discrepancy and fail a
    healthy cutover."""
    await a_migrated_user(db_engine, "migrated")
    async with db_engine.begin() as conn:
        newcomer = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES ('new@example.com', 'mentee', 'Africa/Lagos') RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO credit_lots "
                "(user_id, source, quantity_granted, quantity_remaining, expires_at) "
                "VALUES (:u, 'profile_completed', 1, 1, NULL)"
            ),
            {"u": newcomer},
        )

    result = await run_load(db_engine, [record("migrated", "")])

    assert result.ok


async def test_an_unexplained_lot_is_not_hidden_by_a_starter_for_the_same_user(
    db_engine: AsyncEngine,
) -> None:
    """**One user, two lots, and only one of them explained.**

    The loader never gives somebody both — `''` becomes a starter and a number
    becomes an opening balance, and the two are disjoint. But the API grants a
    starter to anybody who completes onboarding after the extract, so a migrated
    user with credits can hold both by the time this runs.

    Built by merging the two reads into one dict keyed on the anchor, this was
    invisible: the starter's row overwrote the opening balance's and the
    unexplained lot was reported as fine.
    """
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])
    async with db_engine.begin() as conn:
        # The starter the API would grant, with its ledger row — explained.
        await conn.execute(
            text(
                "INSERT INTO credit_lots "
                "(user_id, source, quantity_granted, quantity_remaining, expires_at) "
                "SELECT id, 'profile_completed', 1, 1, NULL FROM users "
                "WHERE legacy_bubble_id = 'holder'"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO credit_transactions (user_id, credit_lot_id, delta, reason) "
                "SELECT l.user_id, l.id, 1, 'grant' FROM credit_lots l JOIN users u "
                "ON u.id = l.user_id WHERE u.legacy_bubble_id = 'holder' "
                "AND l.source = 'profile_completed'"
            )
        )
        # Now break the opening balance's explanation only.
        await conn.execute(
            text(
                "DELETE FROM credit_transactions t USING credit_lots l, users u "
                "WHERE t.credit_lot_id = l.id AND l.user_id = u.id "
                "AND u.legacy_bubble_id = 'holder' AND l.source = 'opening_balance'"
            )
        )

    async with db_engine.begin() as conn:
        finished = await finished_onboarding(conn)
        plan = plan_opening_balances([record("holder", "5")], cutover=CUTOVER, finished=finished)
        result = await reconcile_credits(conn, plan)

    assert result.unexplained == ("holder",)
    assert not result.ok


async def test_every_source_row_is_accounted_for(db_engine: AsyncEngine) -> None:
    for anchor in ("holder", "spent", "never"):
        await a_migrated_user(db_engine, anchor)

    result = await run_load(
        db_engine,
        [record("holder", "5"), record("spent", "0"), record("never", ""), record("x", "five")],
    )

    assert result.unaccounted == ()


# --------------------------------------------------------------------------
# The grandfather — the writer #209's schema was waiting for
# --------------------------------------------------------------------------


async def unlocks_for(engine: AsyncEngine, anchor: str) -> list[Any]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.unlocked_by_referral_id FROM referral_unlocks r "
                "JOIN users u ON u.id = r.user_id WHERE u.legacy_bubble_id = :a"
            ),
            {"a": anchor},
        )
        return [r[0] for r in rows]


async def test_an_active_holder_gets_an_unlock_with_no_referral_behind_it(
    db_engine: AsyncEngine,
) -> None:
    """**`unlocked_by_referral_id` is NULL, and that is why the column is nullable.**

    #209: *"~1,200 migrated users are grandfathered as unlocked and none of them
    ever invited anybody."* A synthetic referral row would put an invite that
    never happened into a table whose whole purpose is evidence.
    """
    await a_migrated_user(db_engine, "holder")

    result = await run_load(db_engine, [record("holder", "5")])

    assert await unlocks_for(db_engine, "holder") == [None]
    assert result.missing_unlocks == ()
    assert result.ok


async def test_a_dormant_holder_gets_no_unlock(db_engine: AsyncEngine) -> None:
    """The owner's condition, and the only thing that stops this grandfathering
    everybody.

    Both halves of the disjunction must miss: last active outside the window
    *and* registered outside it. They keep the balance they had — the lot still
    loads — but the recurring grant does not follow them.
    """
    await a_migrated_user(db_engine, "dormant", active=False)

    result = await run_load(db_engine, [record("dormant", "5")])

    assert await unlocks_for(db_engine, "dormant") == []
    assert await lots_of(db_engine, "dormant") == [("opening_balance", 5, 5, EXPIRY)]
    assert result.ok


async def test_a_spent_down_user_is_still_unlocked(db_engine: AsyncEngine) -> None:
    """Zero means they reached zero, not that they are owed nothing. Keyed off
    the lots instead, this person would be cut off for the accident of having
    spent their last credit before cutover day."""
    await a_migrated_user(db_engine, "spent")

    result = await run_load(db_engine, [record("spent", "0")])

    assert await unlocks_for(db_engine, "spent") == [None]
    assert await lots_of(db_engine, "spent") == []
    assert result.ok


async def test_somebody_who_never_entered_credits_is_still_unlocked(
    db_engine: AsyncEngine,
) -> None:
    """**Every migrated user the platform has seen recently**, balance or not.

    The legacy app granted monthly credits unconditionally, so an empty
    `bookingCredit` says they joined recently rather than that they are owed
    nothing. They get the starter *and* the recurring grant.
    """
    await a_migrated_user(db_engine, "never")

    result = await run_load(db_engine, [record("never", "")])

    assert await unlocks_for(db_engine, "never") == [None]
    assert await lots_of(db_engine, "never") == [("profile_completed", 1, 1, None)]
    assert result.ok


async def test_a_signup_with_no_activity_is_still_unlocked(
    db_engine: AsyncEngine,
) -> None:
    """**The disjunction, and the case an activity-only filter loses.**

    `last_active_at` is null — they registered and never came back — but
    `created_at` falls inside the window. An activity-only filter would cut off
    exactly the new joiner the platform is trying to keep.
    """
    await a_migrated_user(db_engine, "newcomer", active=None)

    result = await run_load(db_engine, [record("newcomer", "2")])

    assert await unlocks_for(db_engine, "newcomer") == [None]
    assert result.ok


async def test_a_second_run_writes_one_unlock(db_engine: AsyncEngine) -> None:
    """`uq_referral_unlocks_user_id` makes the rehearsal-then-cutover path a
    no-op, the same property every other write in this loader has."""
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])

    await run_load(db_engine, [record("holder", "5")])

    assert await unlocks_for(db_engine, "holder") == [None]


async def test_an_unlock_earned_the_ordinary_way_is_not_overwritten(
    db_engine: AsyncEngine,
) -> None:
    """A migrated user who invited somebody between the rehearsal and the cutover
    keeps the unlock they earned, with its referral intact. `DO NOTHING` rather
    than `DO UPDATE` is what preserves the evidence."""
    await a_migrated_user(db_engine, "inviter")
    async with db_engine.begin() as conn:
        referral = (
            await conn.execute(
                text(
                    "INSERT INTO referrals (referrer_id, code, invitee_email, "
                    "signed_up_at, qualified_at) "
                    "SELECT id, 'INVITER1', 'friend@example.com', now(), now() FROM users "
                    "WHERE legacy_bubble_id = 'inviter' RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id) "
                "SELECT id, :r FROM users WHERE legacy_bubble_id = 'inviter'"
            ),
            {"r": referral},
        )

    await run_load(db_engine, [record("inviter", "5")])

    assert await unlocks_for(db_engine, "inviter") == [referral]


async def test_a_missing_unlock_is_reported_by_name(db_engine: AsyncEngine) -> None:
    """The consequence lands a month later and silently — the balance simply
    stops renewing. So the reconciliation names them rather than counting them,
    and refuses to commit."""
    await a_migrated_user(db_engine, "holder")
    await run_load(db_engine, [record("holder", "5")])
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM referral_unlocks r USING users u "
                "WHERE r.user_id = u.id AND u.legacy_bubble_id = 'holder'"
            )
        )

    async with db_engine.begin() as conn:
        finished = await finished_onboarding(conn)
        seen = await recently_seen(conn, since=CUTOVER - ACTIVE_WITHIN)
        plan = plan_opening_balances(
            [record("holder", "5")], cutover=CUTOVER, finished=finished, seen=seen
        )
        result = await reconcile_credits(conn, plan)

    assert result.missing_unlocks == ("holder",)
    assert not result.ok

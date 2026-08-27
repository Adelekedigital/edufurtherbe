"""The expiry sweep, and the boundary it shares with the balance read.

**The test this file exists for is the boundary one.** The sweep and the balance
answer different questions — what happened, and what is spendable — and if they
disagreed about *when* a lot dies, a mentee would see a credit the server would
refuse to spend, or a ledger row for something still on the card. They cannot
disagree, because the sweep asks for `spendable_now` negated rather than for a
second copy of the rule; `test_the_read_and_the_sweep_share_one_boundary` is
what holds that true when somebody rewrites either one.

The second reason this file exists is `delta`. It is the amount that was left,
not the amount that was granted, and the two are equal in every fixture that
never spends anything — so the partially-spent case is the one that discriminates.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db.credit_expiry import expirable_credit_count, expire_credits
from app.infra.db.credit_store import get_credit_summary
from app.infra.db.engine import create_session_factory

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: Midnight opening September — the instant an August lot dies. `end_of_month`
#: is exclusive, so a lot expiring here survives all of 31 August.
MIDNIGHT = dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.UTC)
#: The last instant it is alive. A microsecond, because that is the resolution
#: `timestamptz` keeps and therefore the smallest gap the boundary can have.
JUST_BEFORE = MIDNIGHT - dt.timedelta(microseconds=1)
#: When the scheduled job actually runs — five hours late, deliberately.
SWEEP_TIME = dt.datetime(2026, 9, 1, 5, 0, tzinfo=dt.UTC)


async def a_user(engine: AsyncEngine, *, deleted: bool = False) -> UUID:
    tag = uuid4()
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO users (email, primary_role, timezone, deleted_at) "
                            "VALUES (:e, 'mentee', 'Africa/Lagos', :d) RETURNING id"
                        ),
                        {
                            "e": f"{tag}@example.com",
                            "d": dt.datetime(2026, 1, 1, tzinfo=dt.UTC) if deleted else None,
                        },
                    )
                ).scalar_one()
            )
        )


async def a_lot(
    engine: AsyncEngine,
    user_id: UUID,
    *,
    granted: int = 3,
    remaining: int | None = None,
    expires: dt.datetime | None = MIDNIGHT,
    source: str = "monthly_free",
) -> UUID:
    """A lot **and the grant row that explains it**, the way `conftest.fund` does.

    The second insert is not decoration. `fund` states the rule at the point it
    makes it: *"A lot without one is a balance that rose with nothing saying
    why — the state D8 chose a ledger over a counter to prevent — and every test
    that reconciles the two would be reconciling against a fixture that had
    already broken the rule."*

    That reconciliation is this file's subject. Without the grant row,
    `granted + sum(delta) == remaining` is false before the sweep runs, so
    :func:`reconciles` could never be written and the ETL's Definition of Done
    would go unexercised on the expiry path entirely.

    A partially-spent lot gets its debit too, for the same reason — `remaining`
    below `granted` means credits left, and a fixture that skips the row saying
    where they went is the counter behaviour again.
    """
    left = granted if remaining is None else remaining
    async with engine.begin() as conn:
        lot_id = (
            await conn.execute(
                text(
                    "INSERT INTO credit_lots "
                    "(user_id, source, quantity_granted, quantity_remaining, expires_at) "
                    "VALUES (:u, :s, :g, :r, :e) RETURNING id"
                ),
                {"u": user_id, "s": source, "g": granted, "r": left, "e": expires},
            )
        ).scalar_one()

        await conn.execute(
            text(
                "INSERT INTO credit_transactions "
                "(user_id, credit_lot_id, delta, reason, session_id) "
                "VALUES (:u, :lot, :q, 'grant', NULL)"
            ),
            {"u": user_id, "lot": lot_id, "q": granted},
        )

        if left < granted:
            # Where the spent ones went. `session_booked` needs a session and
            # this fixture has none, so the debit is a `lot_expired` from an
            # earlier period — the only reason in the vocabulary that moves a
            # balance without naming one.
            await conn.execute(
                text(
                    "INSERT INTO credit_transactions "
                    "(user_id, credit_lot_id, delta, reason, session_id) "
                    "VALUES (:u, :lot, :d, 'lot_expired', NULL)"
                ),
                {"u": user_id, "lot": lot_id, "d": left - granted},
            )

        return UUID(str(lot_id))


async def remaining_of(engine: AsyncEngine, lot_id: UUID) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT quantity_remaining FROM credit_lots WHERE id = :l"), {"l": lot_id}
                )
            ).scalar_one()
        )


async def ledger_of(engine: AsyncEngine, user_id: UUID) -> list[tuple[UUID, int, str, UUID | None]]:
    """This user's movements, **including which lot each one names**.

    `credit_lot_id` is in the tuple because without it a delta paired to the
    wrong lot of the same user is invisible: the composite key
    `fk_credit_transactions_lot_belongs_to_user` only checks that the lot is
    *theirs*, not that the amount matches it. A sweep that zipped its deltas
    against a misaligned row list would satisfy every constraint and every
    assertion.

    Ordered by `id` after `created_at`. `created_at` defaults to `now()`, which
    is transaction time — so every row the sweep's single `executemany` writes
    shares one value, and ordering on it alone would be non-deterministic the
    moment a test held two dead lots. `uuid_generate_v7` is time-ordered, so it
    breaks the tie in insertion order.
    """
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT credit_lot_id, delta, reason, session_id FROM credit_transactions "
                "WHERE user_id = :u ORDER BY created_at, id"
            ),
            {"u": user_id},
        )
        return [(row[0], row[1], row[2], row[3]) for row in rows]


async def reconciles(engine: AsyncEngine, lot_id: UUID) -> bool:
    """Whether a lot's own arithmetic holds: granted + every movement == remaining.

    **The invariant the ETL's Definition of Done rests on**, and the one a
    `delta` of the wrong sign, wrong magnitude or wrong lot breaks while every
    other assertion in this file still passes.
    """
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT l.quantity_granted, l.quantity_remaining, "
                    "COALESCE(SUM(t.delta) FILTER (WHERE t.reason <> 'grant'), 0) AS moved "
                    "FROM credit_lots l LEFT JOIN credit_transactions t "
                    "ON t.credit_lot_id = l.id WHERE l.id = :l "
                    "GROUP BY l.quantity_granted, l.quantity_remaining"
                ),
                {"l": lot_id},
            )
        ).one()
    return bool(row[0] + row[2] == row[1])


async def run_sweep(engine: AsyncEngine, *, now: dt.datetime) -> int:
    factory = create_session_factory(engine)
    async with factory() as db:
        expired = await expire_credits(db, now=now)
        await db.commit()
        return expired


async def balance_at(engine: AsyncEngine, user_id: UUID, *, now: dt.datetime) -> int:
    factory = create_session_factory(engine)
    async with factory() as db:
        return (await get_credit_summary(db, user_id, now=now)).balance


# --------------------------------------------------------------------------
# The boundary — the pinning test
# --------------------------------------------------------------------------


async def test_the_read_and_the_sweep_share_one_boundary(db_engine: AsyncEngine) -> None:
    """**Both surfaces, one microsecond apart, on one lot.**

    A lot expiring at midnight is spendable at 23:59:59.999999 and dead at
    midnight. If the sweep carried its own predicate — `expires_at < now()`
    rather than `<=`, which is the off-by-one that looks right — this lot would
    be counted by neither surface at the exact instant it died, and the ledger
    would sit a microsecond behind the balance forever.

    Asserted through `get_credit_summary` rather than against the column,
    because the boundary that matters is the one a mentee sees.
    """
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3)

    # A microsecond early: alive on the card, and the sweep leaves it alone.
    assert await balance_at(db_engine, user, now=JUST_BEFORE) == 3
    await run_sweep(db_engine, now=JUST_BEFORE)
    assert await remaining_of(db_engine, lot) == 3
    assert await ledger_of(db_engine, user) == [(lot, 3, "grant", None)]

    # At the instant itself: gone from the card, and taken by the sweep.
    assert await balance_at(db_engine, user, now=MIDNIGHT) == 0
    await run_sweep(db_engine, now=MIDNIGHT)
    assert await remaining_of(db_engine, lot) == 0
    assert await ledger_of(db_engine, user) == [
        (lot, 3, "grant", None),
        (lot, -3, "lot_expired", None),
    ]
    assert await reconciles(db_engine, lot)


async def test_the_dry_run_counts_what_the_sweep_would_take(db_engine: AsyncEngine) -> None:
    """The script reports before it acts, and the two must ask one question.

    A dry run answering something different from the real run is worse than no
    dry run — the note `unlocked_mentee_count` already carries.

    **An exact count, because `db_engine` is one database per test.**
    `migrated_database` builds `test_<uuid>` from a template and drops it after,
    so nothing another test wrote can be in this number. An earlier draft said
    the opposite and weakened the assertion to `>= 2` on the strength of it — a
    loose bound that would not have noticed a `_dead` predicate which started
    matching lots it should not.

    **Two lots of different sizes, and the ledger read by lot.** This is the
    only fixture here that could catch a delta paired to the wrong row: zip the
    sweep's amounts against a misaligned list and both rows read `-3` against
    lots of 3 and 2, which every constraint permits.
    """
    factory = create_session_factory(db_engine)
    user = await a_user(db_engine)
    big = await a_lot(db_engine, user, granted=3)
    small = await a_lot(db_engine, user, granted=2, expires=JUST_BEFORE, source="referral_unlock")

    async with factory() as db:
        before = await expirable_credit_count(db, now=MIDNIGHT)

    taken = await run_sweep(db_engine, now=MIDNIGHT)

    async with factory() as db:
        assert await expirable_credit_count(db, now=MIDNIGHT) == 0
    assert before == 2
    assert taken == 2

    movements = {
        (lot, delta)
        for lot, delta, reason, _ in await ledger_of(db_engine, user)
        if reason == "lot_expired"
    }
    assert movements == {(big, -3), (small, -2)}
    assert await reconciles(db_engine, big)
    assert await reconciles(db_engine, small)


async def test_a_never_expiring_lot_is_touched_by_neither(db_engine: AsyncEngine) -> None:
    """**The `NULL` half, and the reason the predicate is a disjunct.**

    `NOT (expires_at IS NULL OR expires_at > now())` is `FALSE` for a starter,
    so the sweep cannot reach one. Written as a bare `expires_at <= now()` it
    would be `NULL` — false by accident rather than by construction — and the
    first `NOT` somebody wrapped around it would start expiring every starter
    credit on the platform.
    """
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=1, expires=None, source="profile_completed")

    await run_sweep(db_engine, now=SWEEP_TIME)

    assert await remaining_of(db_engine, lot) == 1
    assert await balance_at(db_engine, user, now=SWEEP_TIME) == 1
    assert await ledger_of(db_engine, user) == [(lot, 1, "grant", None)]


# --------------------------------------------------------------------------
# What the row says
# --------------------------------------------------------------------------


async def test_the_sweep_writes_one_row_saying_what_was_lost(db_engine: AsyncEngine) -> None:
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3)

    await run_sweep(db_engine, now=SWEEP_TIME)

    assert await remaining_of(db_engine, lot) == 0
    assert await ledger_of(db_engine, user) == [
        (lot, 3, "grant", None),
        (lot, -3, "lot_expired", None),
    ]
    assert await reconciles(db_engine, lot)


async def test_the_delta_is_what_was_left_not_what_was_granted(db_engine: AsyncEngine) -> None:
    """**The case that discriminates**, and the only one that does.

    Every other fixture here grants and never spends, so `quantity_granted` and
    `quantity_remaining` are equal and a sweep reading the wrong column passes
    all of them. A mentee who booked two sessions in August loses the one credit
    they had left, not the three they were given — and a ledger claiming three
    would make the lot's own arithmetic wrong: granted 3, movements -2 and -3,
    remaining 0.
    """
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3, remaining=1)

    await run_sweep(db_engine, now=SWEEP_TIME)

    assert await remaining_of(db_engine, lot) == 0
    assert (await ledger_of(db_engine, user))[-1] == (lot, -1, "lot_expired", None)
    assert await reconciles(db_engine, lot)


async def test_an_expiry_names_no_session(db_engine: AsyncEngine) -> None:
    """`ck_credit_transactions_session_matches_reason` is an equivalence, so a
    row that named one would be refused outright — this asserts the writer never
    tries, which is what keeps that constraint a backstop rather than the
    mechanism."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user, granted=2)

    await run_sweep(db_engine, now=SWEEP_TIME)

    assert [row[3] for row in await ledger_of(db_engine, user)] == [None, None]


# --------------------------------------------------------------------------
# Running twice, and running over nothing
# --------------------------------------------------------------------------


async def test_a_second_run_writes_nothing(db_engine: AsyncEngine) -> None:
    """**Self-terminating rather than guarded**, which is why this job ships
    without an index of its own.

    A scheduled job runs twice — a retry, a manual trigger beside the cron. The
    second run finds the lot at zero, and `quantity_remaining > 0` is what stops
    it writing a second `-3`. Without that clause the balance would stay right
    (it is already zero) while the ledger accumulated a fresh row every month,
    forever, for credits that expired once.
    """
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3)
    await run_sweep(db_engine, now=SWEEP_TIME)

    await run_sweep(db_engine, now=SWEEP_TIME + dt.timedelta(hours=1))

    assert await ledger_of(db_engine, user) == [
        (lot, 3, "grant", None),
        (lot, -3, "lot_expired", None),
    ]


async def test_a_fully_spent_lot_writes_nothing(db_engine: AsyncEngine) -> None:
    """Nothing expired, so nothing is recorded. A `delta` of zero is a row
    claiming a balance moved when it did not, and
    `ck_credit_transactions_delta_is_not_zero` would refuse it — so writing one
    would not corrupt the ledger, it would crash the job."""
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3, remaining=0)

    await run_sweep(db_engine, now=SWEEP_TIME)

    # The grant and the earlier debit the fixture wrote, and nothing new.
    assert [reason for _, _, reason, _ in await ledger_of(db_engine, user)] == [
        "grant",
        "lot_expired",
    ]
    assert await reconciles(db_engine, lot)


async def test_a_lot_that_has_not_expired_is_left_alone(db_engine: AsyncEngine) -> None:
    """The accepting half of the schedule: a sweep firing on 1 September must
    not touch the lots the grant creates an hour later, which die on 1 October."""
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3, expires=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))

    await run_sweep(db_engine, now=SWEEP_TIME)

    assert await remaining_of(db_engine, lot) == 3
    assert await ledger_of(db_engine, user) == [(lot, 3, "grant", None)]


# --------------------------------------------------------------------------
# Who it reaches
# --------------------------------------------------------------------------


async def test_a_deleted_users_lot_is_still_swept(db_engine: AsyncEngine) -> None:
    """**The deliberate difference from the monthly grant**, which filters `LIVE`.

    Granting to somebody who is not there is a favour to nobody; expiry is a
    fact about a lot and is true either way. Skipping them would leave un-swept
    lots behind a restored account whose ledger could never explain the gap —
    and the ledger keeps a deleted user's rows precisely because it is evidence
    (ADR 0013).
    """
    user = await a_user(db_engine, deleted=True)
    lot = await a_lot(db_engine, user, granted=2)

    await run_sweep(db_engine, now=SWEEP_TIME)

    assert await remaining_of(db_engine, lot) == 0
    assert await ledger_of(db_engine, user) == [
        (lot, 2, "grant", None),
        (lot, -2, "lot_expired", None),
    ]
    assert await reconciles(db_engine, lot)


# --------------------------------------------------------------------------
# The clock the boundary is read against
# --------------------------------------------------------------------------


async def test_a_naive_now_is_refused(db_engine: AsyncEngine) -> None:
    """**The same refusal `end_of_month` makes, at the other entry point.**

    asyncpg binds a `timestamptz` by calling `astimezone`, and `astimezone` on a
    naive datetime resolves against the *host's* local zone. An operator on
    UTC+1 running this by hand would move the boundary an hour — sweeping lots
    that are still alive, or missing ones that are dead — and the job would
    print a count and exit 0 either way.

    `domain/credits.py` already refuses this input with *"Guessing is how the
    offset gets silently discarded"*. The sweep was the one entry point still
    guessing.
    """
    factory = create_session_factory(db_engine)
    naive = dt.datetime(2026, 9, 1, 0, 0)

    async with factory() as db:
        with pytest.raises(ValueError, match="aware datetime"):
            await expire_credits(db, now=naive)
        with pytest.raises(ValueError, match="aware datetime"):
            await expirable_credit_count(db, now=naive)


async def test_an_aware_now_in_another_zone_reads_the_same_instant(
    db_engine: AsyncEngine,
) -> None:
    """The accepting half, and the one that proves the guard is about the
    *offset* rather than about the tzinfo attribute being present.

    `2026-09-01T01:00+01:00` is `2026-09-01T00:00Z` — the boundary itself. A lot
    dying at midnight UTC is dead when a caller in Lagos names that instant in
    their own zone, because both are the same moment.
    """
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3)

    lagos = dt.datetime(2026, 9, 1, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    await run_sweep(db_engine, now=lagos)

    assert await remaining_of(db_engine, lot) == 0
    assert await reconciles(db_engine, lot)


# --------------------------------------------------------------------------
# The race the lock exists for
# --------------------------------------------------------------------------


async def test_a_spend_landing_mid_sweep_does_not_inflate_the_delta(
    db_engine: AsyncEngine,
) -> None:
    """**Genuinely concurrent, and asserted on the row that gets written.**

    A missing `FOR UPDATE` is invisible: readers do not block on writers under
    MVCC, so the sweep would happily read `quantity_remaining = 3` from a
    snapshot while another transaction was holding that row at 2. It would then
    block on its own `UPDATE`, succeed, and append a row claiming three credits
    expired when two did — a ledger that no longer reconciles against the lot it
    describes, with nothing failing and nothing logged.

    The interleaving has to be exact, so this drives it rather than gathering
    two calls and hoping: the spender takes the row first, the sweep is started
    and left to block on it, and only then does the spender commit.
    """
    user = await a_user(db_engine)
    lot = await a_lot(db_engine, user, granted=3)

    factory = create_session_factory(db_engine)
    async with factory() as spender:
        # Holds the row lock. Not `spend_credit`, which filters on
        # `spendable_now` and would decline to touch an expired lot at all —
        # the point here is a writer that holds the row, whatever its reason.
        #
        # **Its ledger row too**, so the lot still reconciles at the end. A
        # fixture that moves a balance without saying why is the counter
        # behaviour this file exists to rule out, and `reconciles` below would
        # be measuring the fixture's omission rather than the sweep's `delta`.
        await spender.execute(
            text("UPDATE credit_lots SET quantity_remaining = 2 WHERE id = :l"), {"l": lot}
        )
        await spender.execute(
            text(
                "INSERT INTO credit_transactions "
                "(user_id, credit_lot_id, delta, reason, session_id) "
                "VALUES (:u, :l, -1, 'lot_expired', NULL)"
            ),
            {"u": user, "l": lot},
        )

        sweeping = asyncio.create_task(run_sweep(db_engine, now=SWEEP_TIME))
        # Long enough for the sweep to reach its `SELECT … FOR UPDATE` and
        # block. Without this the sweep might not have read yet when the commit
        # lands, and the unlocked version would pass on timing rather than on
        # correctness — which is the whole failure this test is watching for.
        await asyncio.sleep(0.5)

        # **The exception first, and that order matters.** If the sweep raised
        # during the sleep the task is already done, and asserting on
        # `done()` alone would fail naming a lock that was never the problem
        # while the real traceback surfaced only as an "exception never
        # retrieved" warning after the run.
        if sweeping.done():
            sweeping.result()
            pytest.fail("the sweep finished without blocking on the spender's row lock")

        await spender.commit()
        # Bounded, because an unbounded await on a lock that never clears hangs
        # the suite instead of failing it — and this repo runs no timeout
        # plugin, so the job would burn its wall clock before anyone saw why.
        await asyncio.wait_for(sweeping, timeout=30)

    assert await remaining_of(db_engine, lot) == 0
    assert (await ledger_of(db_engine, user))[-1] == (lot, -2, "lot_expired", None)
    assert await reconciles(db_engine, lot)

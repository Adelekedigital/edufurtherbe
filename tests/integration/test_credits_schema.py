"""What the two credit tables guarantee, and what no gate can see.

`alembic check` reads tables, columns, types and regular indexes. Everything
here is outside that set — the quantity CHECKs, two closed vocabularies, two
partial unique indexes whose predicates are the whole point, and foreign keys
whose deletion behaviour is load-bearing because this table is financial
evidence.

Every constraint gets a rejecting **and** an accepting case. A test that only
proves a constraint refuses garbage cannot tell a working constraint from one
that refuses everything — and one of the accepting cases below is the reason
this file exists at all: a booking debit and its refund share a `session_id`,
so an index guarding "one refund per session" that keys on `session_id` alone
would reject the refund it was written to allow.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: Distinguishes "the fixture's session" from an explicit SQL NULL. A default
#: of ``None`` cannot say both, and ``""`` reads as a value rather than an
#: absence — two falsy defaults carrying opposite meanings, in a file whose
#: whole subject is subtle predicate behaviour.
FIXTURE_SESSION = object()

LAGOS = "Africa/Lagos"
STARTS_AT = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)

INSERT_LOT = """
INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining, expires_at)
VALUES (:user, :source, :granted, :remaining, :expires)
RETURNING id
"""

INSERT_TX = """
INSERT INTO credit_transactions (user_id, credit_lot_id, delta, reason, session_id)
VALUES (:user, :lot, :delta, :reason, :session)
RETURNING id
"""


async def make_user(conn: AsyncConnection, email: str, role: str = "mentee") -> str:
    user_id = (
        await conn.execute(
            text(
                "INSERT INTO users (email, primary_role, timezone) "
                "VALUES (:email, :role, :tz) RETURNING id"
            ),
            {"email": email, "role": role, "tz": LAGOS},
        )
    ).scalar_one()
    return str(user_id)


async def make_mentor(conn: AsyncConnection, email: str) -> str:
    user_id = await make_user(conn, email, role="mentor")
    await conn.execute(
        text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Test mentor')"),
        {"u": user_id},
    )
    return user_id


async def insert_session(
    conn: AsyncConnection, mentor_id: str, mentee_id: str, starts_at: datetime = STARTS_AT
) -> str:
    """A second session needs a second hour — `no_mentor_double_booking` is an
    exclusion constraint, so two sessions for one mentor cannot overlap."""
    result = await conn.execute(
        text(
            "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes) "
            "VALUES (:mentor, :mentee, :starts, 45) RETURNING id"
        ),
        {"mentor": mentor_id, "mentee": mentee_id, "starts": starts_at},
    )
    return str(result.scalar_one())


class Ledger:
    """One mentee, one mentor, a session between them, and the connection."""

    def __init__(self, conn: AsyncConnection, mentee: str, mentor: str, session: str) -> None:
        self.conn = conn
        self.mentee = mentee
        self.mentor = mentor
        self.session = session

    async def lot(
        self,
        *,
        source: str = "monthly_free",
        granted: int = 3,
        remaining: int | None = None,
        expires: datetime | None = datetime(2026, 10, 1, tzinfo=UTC),
        user: str | None = None,
    ) -> str:
        result = await self.conn.execute(
            text(INSERT_LOT),
            {
                "user": user or self.mentee,
                "source": source,
                "granted": granted,
                "remaining": granted if remaining is None else remaining,
                "expires": expires,
            },
        )
        return str(result.scalar_one())

    async def transaction(
        self,
        lot: str,
        *,
        delta: int = -1,
        reason: str = "session_booked",
        session: str | object | None = FIXTURE_SESSION,
    ) -> str:
        result = await self.conn.execute(
            text(INSERT_TX),
            {
                "user": self.mentee,
                "lot": lot,
                "delta": delta,
                "reason": reason,
                "session": self.session if session is FIXTURE_SESSION else session,
            },
        )
        return str(result.scalar_one())


@pytest_asyncio.fixture
async def ledger(db_engine: AsyncEngine) -> AsyncIterator[Ledger]:
    async with db_engine.begin() as conn:
        mentor = await make_mentor(conn, "mentor@example.test")
        mentee = await make_user(conn, "mentee@example.test")
        yield Ledger(conn, mentee, mentor, await insert_session(conn, mentor, mentee))


# --------------------------------------------------------------------------
# credit_lots — the quantities
# --------------------------------------------------------------------------


async def test_a_well_formed_lot_is_accepted(ledger: Ledger) -> None:
    assert await ledger.lot()


async def test_a_lot_must_grant_something(ledger: Ledger) -> None:
    """Zero is not a grant, it is a row that says nothing happened."""
    with pytest.raises(IntegrityError, match="quantity_granted"):
        await ledger.lot(granted=0)


async def test_a_balance_cannot_go_negative(ledger: Ledger) -> None:
    """**The guard behind every spend.** The advisory lock serialises one user's
    bookings; this is what catches the case where it does not."""
    with pytest.raises(IntegrityError, match="quantity_remaining"):
        await ledger.lot(granted=3, remaining=-1)


async def test_a_lot_cannot_hold_more_than_it_granted(ledger: Ledger) -> None:
    """A refund that credits the wrong lot would otherwise inflate it silently."""
    with pytest.raises(IntegrityError, match="remaining_lte_granted"):
        await ledger.lot(granted=3, remaining=4)


async def test_a_lot_may_hold_exactly_what_it_granted(ledger: Ledger) -> None:
    """The state every lot is born in, and the boundary the CHECK must admit."""
    assert await ledger.lot(granted=3, remaining=3)


async def test_a_lot_may_be_spent_to_zero(ledger: Ledger) -> None:
    assert await ledger.lot(granted=3, remaining=0)


# --------------------------------------------------------------------------
# credit_lots — the vocabulary and the once-only starter
# --------------------------------------------------------------------------


async def test_the_source_vocabulary_is_closed(ledger: Ledger) -> None:
    """Settled decision #100: `text` plus a CHECK, and the CHECK is the guard.

    `purchase` is the value this asserts against on purpose — it is in the
    canonical DDL, payments are out of scope by decision #8, and #21 names this
    exact enum as its cautionary example.
    """
    with pytest.raises(IntegrityError, match="source_is_known"):
        await ledger.lot(source="purchase")


async def test_every_shipped_source_is_accepted(ledger: Ledger) -> None:
    """The accepting half. A CHECK that refuses everything also refuses garbage."""
    for source in ("profile_completed", "referral_unlock", "monthly_free", "opening_balance"):
        assert await ledger.lot(source=source, granted=1, expires=None)


async def test_a_user_gets_one_starter_ever(ledger: Ledger) -> None:
    """Onboarding completes once, and a re-run of its producer must not pay twice.

    Structural rather than checked in application code, because the producer is
    a transition somebody will eventually make idempotent by retrying it.
    """
    await ledger.lot(source="profile_completed", granted=1, expires=None)

    with pytest.raises(IntegrityError, match="one_starter"):
        await ledger.lot(source="profile_completed", granted=1, expires=None)


async def test_the_starter_index_does_not_constrain_other_sources(ledger: Ledger) -> None:
    """**The predicate is the point.** A unique index on `user_id` alone would
    stop a user ever receiving a second monthly grant."""
    await ledger.lot(source="profile_completed", granted=1, expires=None)

    assert await ledger.lot(source="monthly_free")
    assert await ledger.lot(source="monthly_free")


# --------------------------------------------------------------------------
# credit_transactions — the ledger
# --------------------------------------------------------------------------


async def test_a_well_formed_transaction_is_accepted(ledger: Ledger) -> None:
    assert await ledger.transaction(await ledger.lot())


async def test_a_transaction_must_move_something(ledger: Ledger) -> None:
    """A zero delta is a row claiming a balance changed when it did not."""
    with pytest.raises(IntegrityError, match="delta"):
        await ledger.transaction(await ledger.lot(), delta=0)


async def test_the_reason_vocabulary_is_closed(ledger: Ledger) -> None:
    with pytest.raises(IntegrityError, match="reason_is_known"):
        await ledger.transaction(await ledger.lot(), reason="session_no_show_forfeit")


async def test_every_shipped_reason_is_accepted(ledger: Ledger) -> None:
    lot = await ledger.lot()
    assert await ledger.transaction(lot, delta=3, reason="grant", session=None)
    assert await ledger.transaction(lot, delta=-1, reason="session_booked")
    assert await ledger.transaction(lot, delta=-2, reason="lot_expired", session=None)


# --------------------------------------------------------------------------
# The lot and the movement must belong to the same person
# --------------------------------------------------------------------------


async def test_a_movement_cannot_name_a_lot_belonging_to_someone_else(ledger: Ledger) -> None:
    """**The guard no single-column key can give.**

    `user_id` is denormalised from the lot so a ledger read is one table. The
    cost of that copy is that the two can disagree — and nothing above the
    database would see it: a lot-scoped balance and a transaction-scoped
    balance would each look correctly filtered and return different numbers,
    while a *global* reconciliation of "lot sum equals ledger sum" passes.

    `session_types.conferencing_option_id` met this exact problem and states the
    rule — "a single-column key is satisfied by *any* option row, including
    another mentor's". Substitute one noun.
    """
    stranger = await make_user(ledger.conn, "stranger@example.test")
    theirs = await ledger.lot(user=stranger)

    # `Ledger.transaction` always writes `user_id = self.mentee`, so this row
    # claims the mentee moved a balance held by the stranger.
    with pytest.raises(IntegrityError, match="lot_belongs_to_user"):
        await ledger.transaction(theirs)


async def test_a_movement_against_your_own_lot_is_accepted(ledger: Ledger) -> None:
    """The accepting half, and not a formality.

    A composite key with the columns transposed — `(credit_lot_id, user_id)`
    against `(id, user_id)` — rejects the case above just as loudly and rejects
    this one too. Only the accepting case tells a working constraint from one
    that refuses everything.
    """
    assert await ledger.transaction(await ledger.lot())


# --------------------------------------------------------------------------
# One refund per session — the guard the sweep depends on
# --------------------------------------------------------------------------


async def test_a_session_is_refunded_once(ledger: Ledger) -> None:
    """The sweep re-runs, so this is a constraint rather than a check.

    `settle_attendance` commits per batch and is scheduled; a cancel followed by
    a sweep, or a sweep that runs twice, would otherwise pay twice for one
    session.
    """
    lot = await ledger.lot()
    await ledger.transaction(lot, delta=1, reason="session_no_show_refund")

    with pytest.raises(IntegrityError, match="one_refund_per_session"):
        await ledger.transaction(lot, delta=1, reason="session_cancelled_refund")


async def test_the_debit_and_its_refund_share_a_session(ledger: Ledger) -> None:
    """**The accepting case this file was written for.**

    A booking debit and the refund that reverses it carry the same `session_id`.
    An index keyed on `session_id` alone would reject the refund it exists to
    permit — and every rejecting test above would still pass.
    """
    lot = await ledger.lot()

    await ledger.transaction(lot, delta=-1, reason="session_booked")

    assert await ledger.transaction(lot, delta=1, reason="session_no_show_refund")


async def test_refunds_of_different_sessions_do_not_collide(ledger: Ledger) -> None:
    lot = await ledger.lot()
    other = await insert_session(
        ledger.conn, ledger.mentor, ledger.mentee, datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    )

    await ledger.transaction(lot, delta=1, reason="session_no_show_refund")

    assert await ledger.transaction(lot, delta=1, reason="session_no_show_refund", session=other)


# --------------------------------------------------------------------------
# Deletion policy — this table is evidence
# --------------------------------------------------------------------------


async def test_a_user_holding_credits_cannot_be_hard_deleted(ledger: Ledger) -> None:
    """ADR 0013: restrict where the child is evidence.

    The canonical DDL says `ON DELETE CASCADE`. A ledger whose rows vanish with
    the account cannot answer "I was charged for a session that never ran",
    which is the first of D8's four reasons for the ledger existing. Deletion
    propagation is application logic here, explicit and tested.

    **The holder must own a lot and nothing else.** The fixture's mentee is
    already referenced by `sessions`, so deleting *them* trips
    `fk_sessions_mentee_id_users` first — and this assertion would then be
    reporting on the session FK while claiming to test the ledger's. It cannot
    tell `RESTRICT` from `CASCADE` on `credit_lots` at all, because the delete
    never reaches that constraint either way.
    """
    holder = await make_user(ledger.conn, "holder@example.test")
    await ledger.lot(user=holder)

    with pytest.raises(IntegrityError, match="credit_lots"):
        await ledger.conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": holder})


async def test_a_lot_cannot_reference_a_user_that_does_not_exist(ledger: Ledger) -> None:
    with pytest.raises(IntegrityError, match="user_id"):
        await ledger.lot(user=str(uuid.uuid4()))


# --------------------------------------------------------------------------
# The trigger — an accepting and a rejecting case for a thing no gate can see
# --------------------------------------------------------------------------


async def _triggers_on(conn: AsyncConnection, table: str) -> list[str]:
    result = await conn.execute(
        text(
            "SELECT t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = :table AND NOT t.tgisinternal "
            "ORDER BY t.tgname"
        ),
        {"table": table},
    )
    return [row[0] for row in result]


async def test_the_ledger_carries_no_updated_at_trigger(ledger: Ledger) -> None:
    """**The defect nothing else in this repository can catch.**

    `credit_transactions` is append-only and has no `updated_at` column.
    Attaching `trg_set_updated_at` anyway *succeeds*: PostgreSQL does not
    validate a trigger function's body at `CREATE TRIGGER`, so `set_updated_at()`
    assigning `NEW.updated_at` would raise only at the first `UPDATE` — which an
    append-only table never receives.

    Every other gate stays green through that: `alembic check` is blind to
    triggers, the model test asserts the *column* is absent rather than the
    trigger, and no test issues an `UPDATE` against this table. So the assertion
    has to be made here, directly, or the mistake ships and waits.
    """
    assert await _triggers_on(ledger.conn, "credit_transactions") == []


async def test_the_lots_table_does_carry_one(ledger: Ledger) -> None:
    """The accepting half, and not a formality.

    An assertion that a trigger is absent passes just as well against a
    migration that forgot to create *either* one — at which point `credit_lots`
    silently stops maintaining `updated_at`. Asserting the pair is what tells a
    deliberate omission from a wholesale one.
    """
    assert await _triggers_on(ledger.conn, "credit_lots") == ["trg_set_updated_at"]


async def test_a_user_gets_one_opening_balance_ever(ledger: Ledger) -> None:
    """**The migration's loader runs more than once by design.**

    A rehearsal, a partial load retried, then the real cutover. Without this
    every migrated user collects a second lot and their balance silently
    doubles — and the ETL's Definition of Done requires a second run to produce
    identical rows.
    """
    await ledger.lot(source="opening_balance", granted=5, expires=None)

    with pytest.raises(IntegrityError, match="one_opening_balance"):
        await ledger.lot(source="opening_balance", granted=5, expires=None)


async def test_the_opening_balance_index_does_not_constrain_other_sources(
    ledger: Ledger,
) -> None:
    """The predicate is the point, exactly as it is for the starter: on
    `user_id` alone a migrated user could never receive a monthly grant."""
    await ledger.lot(source="opening_balance", granted=5, expires=None)

    assert await ledger.lot(source="monthly_free")
    assert await ledger.lot(source="monthly_free")


async def test_a_session_bearing_reason_must_name_a_session(ledger: Ledger) -> None:
    """**A NULL bypasses the one-refund-per-session index entirely.**

    PostgreSQL treats nulls as distinct, so two refunds with no session both
    insert and the guarantee the scheduled sweep leans on evaporates. The same
    hole lets a `session_booked` debit name nothing, which makes D8's first
    question — *"I was charged for a session that never ran"* — unanswerable.
    """
    lot = await ledger.lot()

    with pytest.raises(IntegrityError, match="session_matches_reason"):
        await ledger.transaction(lot, delta=-1, reason="session_booked", session=None)


async def test_a_grant_may_not_name_a_session(ledger: Ledger) -> None:
    """The equivalence cuts both ways: a grant belongs to no session, so
    claiming one would attribute a credit to a booking that did not produce
    it."""
    lot = await ledger.lot()

    with pytest.raises(IntegrityError, match="session_matches_reason"):
        await ledger.transaction(lot, delta=3, reason="grant")


async def test_both_halves_are_still_accepted(ledger: Ledger) -> None:
    """The accepting case. An equivalence written the wrong way round rejects
    the two above just as loudly and rejects these too."""
    lot = await ledger.lot()

    assert await ledger.transaction(lot, delta=3, reason="grant", session=None)
    assert await ledger.transaction(lot, delta=-1, reason="session_booked")

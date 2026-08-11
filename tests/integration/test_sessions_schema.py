"""What the M4 session tables guarantee, and what no gate can see.

`alembic check` reads tables, columns, types and regular indexes. Everything this
file asserts is outside that set — three CHECK constraints, two uniqueness rules
demoted from primary keys, five partial index predicates, the absence of a column
and a trigger, and the deletion behaviour of four foreign keys. A green migration
check says the chain applied, not that any of this holds.

Deliberately **not** re-asserted here, because a second representation of one
rule is non-negotiable #8 rather than extra safety: the `updated_at` trigger
sweep (`test_reference_data`), surrogate primary keys, enum labels, CHECK naming
and the retained-table cascade property (all `test_schema_parity`).

The `no_mentor_double_booking` exclusion constraint is **not tested here** — it
lands in the next revision, and its tests come with it.

Every constraint gets a rejecting **and** an accepting case. A test that only
proves a constraint refuses garbage cannot tell a working constraint from one
that refuses everything.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LAGOS = "Africa/Lagos"
STARTS_AT = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)

#: Deliberately omits `status` so the column default is what gets exercised.
INSERT_SESSION = """
INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes)
VALUES (:mentor, :mentee, :starts, :duration)
RETURNING id
"""

INSERT_WITH_LEGACY = """
INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, legacy_bubble_id)
VALUES (:mentor, :mentee, :starts, :duration, :legacy)
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
    """A user with a mentor profile, which `session_types` requires."""
    user_id = await make_user(conn, email, role="mentor")
    await conn.execute(
        text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Test mentor')"),
        {"u": user_id},
    )
    return user_id


@pytest_asyncio.fixture
async def booking(db_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, str, str]]:
    """One mentor, one mentee, and the connection their rows live on."""
    async with db_engine.begin() as conn:
        yield (
            conn,
            await make_mentor(conn, "mentor@example.test"),
            await make_user(conn, "mentee@example.test"),
        )


async def insert_session(
    conn: AsyncConnection,
    mentor_id: str,
    mentee_id: str,
    *,
    duration: int = 45,
    starts_at: datetime = STARTS_AT,
    legacy_id: str | None = None,
) -> str:
    """Two static statements rather than one assembled from parts.

    Building the column list at runtime trips ruff's S608, and the rule is right
    even though every value here is bound: an f-string is the shape that becomes
    an injection the first time a caller passes something it did not construct.
    The minimal form also keeps `test_a_session_starts_awaiting_the_mentor`
    honest — a `COALESCE` default in the helper would pass whether or not the
    *column* carries one.
    """
    result = await conn.execute(
        text(INSERT_WITH_LEGACY if legacy_id is not None else INSERT_SESSION),
        {
            "mentor": mentor_id,
            "mentee": mentee_id,
            "starts": starts_at,
            "duration": duration,
        }
        | ({"legacy": legacy_id} if legacy_id is not None else {}),
    )
    return str(result.scalar_one())


async def insert_participant(
    conn: AsyncConnection, session_id: str, user_id: str, role: str
) -> None:
    await conn.execute(
        text(
            "INSERT INTO session_participants (session_id, user_id, role) "
            "VALUES (:s, :u, CAST(:role AS session_role))"
        ),
        {"s": session_id, "u": user_id, "role": role},
    )


async def insert_session_type(conn: AsyncConnection, mentor_id: str, name: str) -> str:
    result = await conn.execute(
        text("INSERT INTO session_types (mentor_user_id, name) VALUES (:m, :n) RETURNING id"),
        {"m": mentor_id, "n": name},
    )
    return str(result.scalar_one())


async def index_definition(engine: AsyncEngine, name: str) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n"),
            {"n": name},
        )
        return str(result.scalar_one())


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


async def test_a_well_formed_session_is_accepted(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    conn, mentor_id, mentee_id = booking

    session_id = await insert_session(conn, mentor_id, mentee_id)

    assert session_id


async def test_a_session_starts_awaiting_the_mentor(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The default is a lifecycle position, not a placeholder.

    A session exists before anyone attends and before the mentor has decided,
    which is package D5's point: status is never derived from attendance.
    """
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)

    status = await conn.execute(
        text("SELECT status FROM sessions WHERE id = :i"), {"i": session_id}
    )

    assert status.scalar_one() == "pending_mentor_approval"


async def test_a_mentor_may_not_book_themselves(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    conn, mentor_id, _ = booking

    with pytest.raises(IntegrityError, match="no_self_booking"):
        await insert_session(conn, mentor_id, mentor_id)


@pytest.mark.parametrize("duration", [5, 480])
async def test_both_ends_of_the_duration_range_are_legal(
    booking: tuple[AsyncConnection, str, str], duration: int
) -> None:
    """The accepting half. Without it a constraint refusing everything passes."""
    conn, mentor_id, mentee_id = booking

    assert await insert_session(conn, mentor_id, mentee_id, duration=duration)


@pytest.mark.parametrize("duration", [4, 481, 0, -45])
async def test_a_duration_outside_the_range_is_refused(
    booking: tuple[AsyncConnection, str, str], duration: int
) -> None:
    """4 and 481 are the off-by-one cases; 0 and a negative are the nonsense ones.

    The dev export contains two tracker rows carrying `Session Duration` of `1`
    and `4`, so this constraint refuses real legacy data — which is why the
    transform has to quarantine them rather than discover it at load time.
    """
    conn, mentor_id, mentee_id = booking

    with pytest.raises(IntegrityError, match="duration_minutes_valid"):
        await insert_session(conn, mentor_id, mentee_id, duration=duration)


async def test_the_legacy_anchor_is_unique(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The ETL is idempotent on this column; a duplicate would double-load."""
    conn, mentor_id, mentee_id = booking
    await insert_session(conn, mentor_id, mentee_id, legacy_id="1757173469027x734817145088051000")

    with pytest.raises(IntegrityError, match="legacy_bubble_id"):
        await insert_session(
            conn, mentor_id, mentee_id, legacy_id="1757173469027x734817145088051000"
        )


# --------------------------------------------------------------------------
# session_participants
# --------------------------------------------------------------------------


async def test_one_person_appears_once_per_session(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The invariant the package's composite primary key carried."""
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)
    await insert_participant(conn, session_id, mentee_id, "mentee")

    with pytest.raises(IntegrityError, match="uq_session_participants_session_id"):
        await insert_participant(conn, session_id, mentee_id, "observer")


async def test_the_same_person_may_attend_another_session(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The accepting half: uniqueness is per pair, not per person."""
    conn, mentor_id, mentee_id = booking
    first = await insert_session(conn, mentor_id, mentee_id)
    second = await insert_session(
        conn, mentor_id, mentee_id, starts_at=datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    )

    await insert_participant(conn, first, mentee_id, "mentee")
    await insert_participant(conn, second, mentee_id, "mentee")

    count = await conn.execute(
        text("SELECT count(*) FROM session_participants WHERE user_id = :u"), {"u": mentee_id}
    )
    assert count.scalar_one() == 2


async def test_a_session_has_at_most_one_mentor(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """Catches drift between `sessions.mentor_id` and the participant rows.

    Two mentor rows would make "who hosted this" unanswerable, and the session
    row itself could no longer be trusted to agree with attendance.
    """
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)
    await insert_participant(conn, session_id, mentor_id, "mentor")
    other_mentor = await make_mentor(conn, "second@example.test")

    with pytest.raises(IntegrityError, match="ix_session_participants_one_mentor"):
        await insert_participant(conn, session_id, other_mentor, "mentor")


async def test_a_session_may_hold_several_mentees(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The index is partial, so it constrains mentors and nothing else.

    Group sessions do not exist yet and every legacy record is 1:1 — but the
    constraint that will have to change when they arrive is the *exclusion*
    constraint, not this one. This asserts the participant table is already the
    right shape, so the later change is one constraint rather than two.
    """
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)
    second_mentee = await make_user(conn, "second-mentee@example.test")

    await insert_participant(conn, session_id, mentee_id, "mentee")
    await insert_participant(conn, session_id, second_mentee, "mentee")

    count = await conn.execute(
        text("SELECT count(*) FROM session_participants WHERE session_id = :s"), {"s": session_id}
    )
    assert count.scalar_one() == 2


async def test_attendance_starts_pending(booking: tuple[AsyncConnection, str, str]) -> None:
    """ "We do not know yet" is a real answer and is not `no_show`."""
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)
    await insert_participant(conn, session_id, mentee_id, "mentee")

    status = await conn.execute(
        text("SELECT attendance_status FROM session_participants WHERE session_id = :s"),
        {"s": session_id},
    )
    assert status.scalar_one() == "pending"


# --------------------------------------------------------------------------
# session_type_booking_configs
# --------------------------------------------------------------------------


async def test_a_session_type_has_at_most_one_booking_config(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The 1:1 invariant the package expressed as a primary key."""
    conn, mentor_id, _ = booking
    type_id = await insert_session_type(conn, mentor_id, "General Mentorship")
    await conn.execute(
        text(
            "INSERT INTO session_type_booking_configs (session_type_id, duration_minutes) "
            "VALUES (:t, 45)"
        ),
        {"t": type_id},
    )

    with pytest.raises(IntegrityError, match="uq_session_type_booking_configs_session_type_id"):
        await conn.execute(
            text(
                "INSERT INTO session_type_booking_configs (session_type_id, duration_minutes) "
                "VALUES (:t, 60)"
            ),
            {"t": type_id},
        )


async def test_another_session_type_carries_its_own_config(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The accepting half, and the reason duration lives here at all.

    A 15-minute question and a 60-minute document review share no meaningful
    default, which is why duration is a property of the offering rather than of
    the mentor (settled decision #58).
    """
    conn, mentor_id, _ = booking
    quick = await insert_session_type(conn, mentor_id, "Quick question")
    deep = await insert_session_type(conn, mentor_id, "Document review")

    for type_id, minutes in ((quick, 15), (deep, 60)):
        await conn.execute(
            text(
                "INSERT INTO session_type_booking_configs (session_type_id, duration_minutes) "
                "VALUES (:t, :d)"
            ),
            {"t": type_id, "d": minutes},
        )

    count = await conn.execute(text("SELECT count(*) FROM session_type_booking_configs"))
    assert count.scalar_one() == 2


async def test_a_config_duration_outside_the_range_is_refused(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    conn, mentor_id, _ = booking
    type_id = await insert_session_type(conn, mentor_id, "General Mentorship")

    with pytest.raises(IntegrityError, match="duration_minutes_valid"):
        await conn.execute(
            text(
                "INSERT INTO session_type_booking_configs (session_type_id, duration_minutes) "
                "VALUES (:t, 4)"
            ),
            {"t": type_id},
        )


# --------------------------------------------------------------------------
# session_events — the append-only log
# --------------------------------------------------------------------------


async def test_the_event_log_carries_no_updated_at(db_engine: AsyncEngine) -> None:
    """A fact that can be edited is not a log.

    The trigger sweep in `test_reference_data` only inspects tables that *have*
    `updated_at`, so a column added here by copying a neighbour would be invisible
    to it. This is the assertion that notices.
    """
    async with db_engine.connect() as conn:
        columns = await conn.execute(
            text(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid = 'session_events'::regclass AND attnum > 0 AND NOT attisdropped"
            )
        )
        names = {row.attname for row in columns}

    assert "created_at" in names
    assert "updated_at" not in names


async def test_the_event_log_has_no_updated_at_trigger(db_engine: AsyncEngine) -> None:
    """The migration attaches the trigger by looping a list, which invites a copy."""
    async with db_engine.connect() as conn:
        triggers = await conn.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'session_events'::regclass AND NOT tgisinternal"
            )
        )
        names = {row.tgname for row in triggers}

    assert names == set()


async def test_an_event_defaults_to_a_user_actor_and_empty_metadata(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)

    await conn.execute(
        text(
            "INSERT INTO session_events (session_id, to_status) "
            "VALUES (:s, CAST('pending_mentor_approval' AS session_status))"
        ),
        {"s": session_id},
    )

    row = await conn.execute(
        text(
            "SELECT actor_id, actor_type, reason_code, metadata FROM session_events "
            "WHERE session_id = :s"
        ),
        {"s": session_id},
    )
    actor_id, actor_type, reason_code, metadata = row.one()

    assert actor_id is None
    assert actor_type == "user"
    assert reason_code is None
    assert metadata == {}


# --------------------------------------------------------------------------
# Deletion policy — ADR 0013, one edge at a time
# --------------------------------------------------------------------------


async def test_a_user_with_a_session_cannot_be_deleted(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """`sessions.mentee_id` restricts, where the package left it to default.

    The mentee is deleted rather than the mentor because a mentor also owns a
    `mentor_profiles` row, and a cascade through that would prove a different
    edge. This isolates the one under test.
    """
    conn, mentor_id, mentee_id = booking
    await insert_session(conn, mentor_id, mentee_id)

    with pytest.raises(IntegrityError, match="fk_sessions_mentee_id_users"):
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": mentee_id})


async def test_deleting_a_session_takes_its_participants(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """Attendance is part of the session record, not an independent claim."""
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)
    await insert_participant(conn, session_id, mentee_id, "mentee")

    await conn.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": session_id})

    count = await conn.execute(text("SELECT count(*) FROM session_participants"))
    assert count.scalar_one() == 0


async def test_a_session_with_events_cannot_be_deleted(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """The audit trail outranks the row it describes.

    The package cascades here. ADR 0013's rule is that evidence restricts, and
    `test_schema_parity` additionally requires that no cascade path from `users`
    reaches `session_events` — this is the edge that keeps both true.
    """
    conn, mentor_id, mentee_id = booking
    session_id = await insert_session(conn, mentor_id, mentee_id)
    await conn.execute(
        text(
            "INSERT INTO session_events (session_id, to_status) "
            "VALUES (:s, CAST('cancelled' AS session_status))"
        ),
        {"s": session_id},
    )

    with pytest.raises(IntegrityError, match="fk_session_events_session_id_sessions"):
        await conn.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": session_id})


async def test_a_mentor_profile_with_a_session_type_cannot_be_deleted(
    booking: tuple[AsyncConnection, str, str],
) -> None:
    """Restricts where `availability_rules` cascades, against the same parent.

    A session type is referenced by `sessions`, which is retained. Cascading it
    would leave a mentor-profile delete to be blocked by a *second* foreign key
    rather than by the one a reader is looking at.
    """
    conn, mentor_id, _ = booking
    await insert_session_type(conn, mentor_id, "General Mentorship")

    with pytest.raises(IntegrityError, match="fk_session_types_mentor_user_id_mentor_profiles"):
        await conn.execute(text("DELETE FROM mentor_profiles WHERE user_id = :u"), {"u": mentor_id})


# --------------------------------------------------------------------------
# The objects `alembic check` cannot see
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index_name", "column"),
    [("ix_sessions_mentor_upcoming", "mentor_id"), ("ix_sessions_mentee_upcoming", "mentee_id")],
)
async def test_the_upcoming_indexes_only_carry_live_sessions(
    db_engine: AsyncEngine, index_name: str, column: str
) -> None:
    """The predicate is the whole point and is invisible to the gate.

    Without the WHERE the index still applies and still speeds the query up, so
    nothing fails — it simply carries every cancelled session forever, which on
    this data is 92 rows in 105.
    """
    sql = await index_definition(db_engine, index_name)

    assert "WHERE" in sql
    assert column in sql
    assert "starts_at" in sql
    assert "pending_mentor_approval" in sql
    assert "confirmed" in sql


async def test_the_completed_index_carries_only_completed_sessions(
    db_engine: AsyncEngine,
) -> None:
    sql = await index_definition(db_engine, "ix_sessions_mentor_completed")

    assert "WHERE" in sql
    assert "completed" in sql


async def test_the_one_mentor_index_is_unique_and_partial(db_engine: AsyncEngine) -> None:
    """Both halves. Unique without the predicate would forbid a second *mentee*."""
    sql = await index_definition(db_engine, "ix_session_participants_one_mentor")

    assert "CREATE UNIQUE INDEX" in sql
    assert "WHERE" in sql
    assert "mentor" in sql


async def test_the_reason_index_skips_events_with_no_code(db_engine: AsyncEngine) -> None:
    """Every migrated event carries a null code, so the partial half is most of it."""
    sql = await index_definition(db_engine, "ix_session_events_reason")

    assert "WHERE" in sql
    assert "reason_code IS NOT NULL" in sql


async def test_the_session_type_index_excludes_inactive_and_deleted_rows(
    db_engine: AsyncEngine,
) -> None:
    sql = await index_definition(db_engine, "ix_session_types_mentor")

    assert "WHERE" in sql
    assert "is_active" in sql
    assert "deleted_at IS NULL" in sql

"""Loading the five session tables, twice, with Bubble's timestamps intact.

**The second run is the test.** ``trg_set_updated_at`` is a ``BEFORE UPDATE``
trigger, so a first load into empty tables preserves Bubble's timestamps whether
or not anything disables it. It is the re-run, taking the ``DO UPDATE`` branch,
that rewrites every migrated ``updated_at`` to the import clock — and the ETL's
recovery plan *is* the re-run, so that is the normal path rather than the
exceptional one.

Two of the five tables have no key to upsert on, and they fail differently:

* ``session_types`` gets one from ``ix_session_types_mentor_name``. The
  ``ON CONFLICT`` must repeat that index's ``WHERE deleted_at IS NULL``, and
  omitting it does not fall back to another index — it raises.
* ``session_events`` has none at all, so the loader deletes its own rows and
  writes them again. A re-run that duplicated events would leave every count
  right except that one.

The other thing only a database can show is that ``session_type_id`` is populated
on every row. The column is nullable today, so a null lands looking entirely
valid and nothing but this notices — and zero nulls is the precondition for the
later revision that makes it ``NOT NULL``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    MeetingProvider,
    SessionRole,
    SessionStatus,
)
from app.domain.transform.sessions import (
    ParticipantRow,
    SessionEventRow,
    SessionPlan,
    SessionRow,
    SessionTypeRow,
)
from app.infra.etl.sessions import UPSERT_CONFERENCING_OPTION, SessionLoader
from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MENTOR = "1720393627919x464416579629646660"
MENTEE = "1751061686104x253709313178114300"
OTHER = "1749506597973x508378565841367300"

#: Deliberately in the past. `now()` would be indistinguishable from the import
#: clock, which is the thing under test.
BUBBLE_CREATED = datetime(2025, 9, 6, 15, 44, tzinfo=UTC)
BUBBLE_MODIFIED = datetime(2025, 9, 7, 13, 0, tzinfo=UTC)
STARTS_AT = datetime(2025, 9, 8, 5, 0, tzinfo=UTC)


def session_row(**overrides: object) -> SessionRow:
    values: dict[str, object] = {
        "legacy_bubble_id": "sb-1",
        "mentor_bubble_id": MENTOR,
        "mentee_bubble_id": MENTEE,
        "status": SessionStatus.CANCELLED,
        "starts_at": STARTS_AT,
        "duration_minutes": 45,
        "topic": "Statement of Purpose",
        "booking_message": "please help",
        "meeting_provider": MeetingProvider.GOOGLE_MEET,
        "meeting_url": "https://meet.google.com/ohd-gsnt-rbj",
        "external_room_id": None,
        "external_calendar_event_id": None,
        "created_at": BUBBLE_CREATED,
        "updated_at": BUBBLE_MODIFIED,
        "from_orphan_tracker": False,
    }
    values.update(overrides)
    return SessionRow(**values)  # type: ignore[arg-type]


def plan_of(**overrides: object) -> SessionPlan:
    values: dict[str, object] = {
        "sessions": (session_row(),),
        "participants": (
            ParticipantRow("sb-1", MENTOR, SessionRole.MENTOR, None, AttendanceStatus.NO_SHOW),
            ParticipantRow("sb-1", MENTEE, SessionRole.MENTEE, None, AttendanceStatus.NO_SHOW),
        ),
        "events": (
            SessionEventRow(
                "sb-1",
                None,
                SessionStatus.PENDING_MENTOR_APPROVAL,
                MENTEE,
                ActorType.USER,
                None,
                BUBBLE_CREATED,
            ),
            SessionEventRow(
                "sb-1",
                SessionStatus.PENDING_MENTOR_APPROVAL,
                SessionStatus.CANCELLED,
                MENTEE,
                ActorType.USER,
                "no longer needed",
                BUBBLE_MODIFIED,
            ),
        ),
        "session_types": (SessionTypeRow(MENTOR, "General Mentorship", 45, MeetingProvider.DAILY),),
        "source_booking_ids": ("sb-1",),
    }
    values.update(overrides)
    return SessionPlan(**values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def seeded(db_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, dict[str, UUID]]]:
    """A mentor with a profile, two mentees, and the id map the loader takes."""
    async with db_engine.begin() as conn:
        users: dict[str, UUID] = {}
        for bubble_id, email, role in (
            (MENTOR, "mentor@example.test", "mentor"),
            (MENTEE, "mentee@example.test", "mentee"),
            (OTHER, "other@example.test", "mentee"),
        ):
            users[bubble_id] = await add_user(
                conn,
                email,
                primary_role=role,
                timezone="Africa/Lagos",
                legacy_bubble_id=bubble_id,
            )
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'M')"),
            {"u": users[MENTOR]},
        )
        yield conn, users


async def counts(conn: AsyncConnection) -> dict[str, int]:
    result = {}
    for table in (
        "sessions",
        "session_participants",
        "session_events",
        "session_types",
        "session_type_booking_configs",
    ):
        row = await conn.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        result[table] = row.scalar_one()
    return result


# --------------------------------------------------------------------------


async def test_a_first_load_writes_every_table(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    conn, users = seeded
    await SessionLoader(conn).load(users=users, plan=plan_of())

    assert await counts(conn) == {
        "sessions": 1,
        "session_participants": 2,
        "session_events": 2,
        "session_types": 1,
        "session_type_booking_configs": 1,
    }


async def test_a_second_load_changes_nothing(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Idempotency is the recovery plan, so this is the test that matters.

    Two of the five tables have no natural key — a re-run that duplicated
    session types or events would leave every other count correct.
    """
    conn, users = seeded
    loader = SessionLoader(conn)
    await loader.load(users=users, plan=plan_of())
    first = await counts(conn)

    await loader.load(users=users, plan=plan_of())

    assert await counts(conn) == first


async def test_bubble_timestamps_survive_the_second_load(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The trigger is unconditional, so only the hold-off keeps these."""
    conn, users = seeded
    loader = SessionLoader(conn)
    await loader.load(users=users, plan=plan_of())
    await loader.load(users=users, plan=plan_of())

    row = await conn.execute(
        text("SELECT created_at, updated_at FROM sessions WHERE legacy_bubble_id = 'sb-1'")
    )
    created, updated = row.one()

    assert created == BUBBLE_CREATED
    assert updated == BUBBLE_MODIFIED


async def test_every_loaded_session_carries_a_session_type(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The precondition for the later NOT NULL revision.

    The column is nullable today, so a null would land looking entirely valid.
    """
    conn, users = seeded
    await SessionLoader(conn).load(users=users, plan=plan_of())

    row = await conn.execute(text("SELECT count(*) FROM sessions WHERE session_type_id IS NULL"))
    assert row.scalar_one() == 0


async def test_events_are_replaced_rather_than_appended(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """`session_events` has no key to upsert on, so the loader deletes its own.

    Asserted by changing the plan's events between runs: appending would leave
    three, and a delete scoped too widely would leave one.
    """
    conn, users = seeded
    loader = SessionLoader(conn)
    await loader.load(users=users, plan=plan_of())

    single = (
        SessionEventRow(
            "sb-1",
            None,
            SessionStatus.PENDING_MENTOR_APPROVAL,
            MENTEE,
            ActorType.USER,
            None,
            BUBBLE_CREATED,
        ),
    )
    await loader.load(users=users, plan=plan_of(events=single))

    row = await conn.execute(text("SELECT count(*) FROM session_events"))
    assert row.scalar_one() == 1


async def test_another_sessions_events_are_left_alone(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The delete is scoped to the plan. A loader that cleared the table would
    pass every count check above and silently destroy a prior phase's rows."""
    conn, users = seeded
    loader = SessionLoader(conn)
    await loader.load(users=users, plan=plan_of())

    second = session_row(
        legacy_bubble_id="sb-2",
        mentee_bubble_id=OTHER,
        starts_at=datetime(2025, 9, 9, 5, 0, tzinfo=UTC),
    )
    await loader.load(
        users=users,
        plan=plan_of(
            sessions=(second,),
            participants=(),
            events=(
                SessionEventRow(
                    "sb-2",
                    None,
                    SessionStatus.PENDING_MENTOR_APPROVAL,
                    OTHER,
                    ActorType.USER,
                    None,
                    BUBBLE_CREATED,
                ),
            ),
            source_booking_ids=("sb-2",),
        ),
    )

    row = await conn.execute(text("SELECT count(*) FROM session_events"))
    assert row.scalar_one() == 3, "sb-1's two events must survive sb-2's load"


async def test_an_unresolvable_user_raises_rather_than_skipping(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """Skipping would drop a session with nothing to show for it."""
    conn, users = seeded

    with pytest.raises(LookupError, match="unknown user"):
        await SessionLoader(conn).load(
            users=users, plan=plan_of(sessions=(session_row(mentee_bubble_id="nobody"),))
        )


async def test_the_upsert_requires_the_index_predicate(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """`ON CONFLICT` infers a partial index only when it repeats the predicate.

    Measured rather than assumed, and pinned here because the failure is at
    runtime on the *second* run: omitting `WHERE deleted_at IS NULL` does not
    quietly choose another index, it raises.
    """
    conn, users = seeded
    await SessionLoader(conn).load(users=users, plan=plan_of())

    with pytest.raises(Exception, match="no unique or exclusion constraint"):
        await conn.execute(
            text(
                "INSERT INTO session_types (mentor_user_id, name) "
                "VALUES (:m, 'General Mentorship') "
                "ON CONFLICT (mentor_user_id, name) DO NOTHING"
            ),
            {"m": users[MENTOR]},
        )


async def test_a_retired_type_does_not_block_a_new_one(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The index is partial on `deleted_at IS NULL` for exactly this.

    **A pointer-release line stood here and is deleted with the pointer.** Under
    D88 the loader named a primary offering and
    `trg_refuse_retiring_a_primary_offering` refused to retire one still pointed
    at, so this test raised `RestrictViolationError` rather than reporting a
    wrong count until the pointer was released first. Both the guard and the
    pointer are gone, so the line guarded nothing and would have sat here as
    setup that reads as meaningful and does nothing — which is worse than absent,
    because the next reader has to work out that it is dead.
    """
    conn, users = seeded
    await SessionLoader(conn).load(users=users, plan=plan_of())
    await conn.execute(text("UPDATE session_types SET deleted_at = now()"))

    await conn.execute(
        text("INSERT INTO session_types (mentor_user_id, name) VALUES (:m, 'General Mentorship')"),
        {"m": users[MENTOR]},
    )

    row = await conn.execute(text("SELECT count(*) FROM session_types"))
    assert row.scalar_one() == 2


async def test_two_live_types_with_one_name_are_refused(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The product half of the invariant, not just the ETL's key."""
    conn, users = seeded
    await SessionLoader(conn).load(users=users, plan=plan_of())

    with pytest.raises(IntegrityError, match="ix_session_types_mentor_name"):
        await conn.execute(
            text(
                "INSERT INTO session_types (mentor_user_id, name) VALUES (:m, 'General Mentorship')"
            ),
            {"m": users[MENTOR]},
        )


# --------------------------------------------------------------------------
# The config carries the venue, and inherits the confirmation
# --------------------------------------------------------------------------


async def test_the_load_makes_the_venue_the_mentors_default_not_a_pin(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """**The venue arrives on `SessionTypeRow` and lands as the mentor's default.**

    The loader *creates* the option the mentor owns, since a database built
    fresh — migrate, then load — would otherwise put them on the platform
    fallback instead of their real choice.

    **The offering is not pointed at it** (owner, 2026-10-21). A pin was never
    the mentor's, no screen shows one, and it kept the offering on the old venue
    after the mentor switched default — the defect `b2d6f9a1c4e7` cleared.
    """
    conn, users = seeded

    await SessionLoader(conn).load(users=users, plan=plan_of())

    option = (
        await conn.execute(
            text("SELECT provider, is_default, source FROM mentor_conferencing_options")
        )
    ).one()
    assert (option.provider, option.is_default, option.source) == ("daily", True, "import")
    pointer = await conn.execute(text("SELECT conferencing_option_id FROM session_types"))
    assert pointer.scalar_one() is None


async def test_a_mentor_who_never_chose_gets_no_option(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """A blank legacy venue is "never chose" (owner, 2026-10-01): no option row,
    and the offering points at nothing, so it follows the platform default and
    `GET /me/conferencing` reports `is_default_choice: true`. It used to create a
    `google_meet` default that read as the mentor's own choice."""
    conn, users = seeded
    plan = plan_of(session_types=(SessionTypeRow(MENTOR, "General Mentorship", 45, None),))

    await SessionLoader(conn).load(users=users, plan=plan)

    options = await conn.execute(text("SELECT count(*) FROM mentor_conferencing_options"))
    assert options.scalar_one() == 0
    pointer = await conn.execute(text("SELECT conferencing_option_id FROM session_types"))
    assert pointer.scalar_one() is None


@pytest.mark.parametrize("venue", [MeetingProvider.DAILY, None])
async def test_a_reload_clears_a_pin_an_older_load_left(
    seeded: tuple[AsyncConnection, dict[str, UUID]], venue: MeetingProvider | None
) -> None:
    """The load converges on the plan, with a venue or without: a pointer an
    older load wrote goes on the next run, so the offering follows the default."""
    conn, users = seeded
    loader = SessionLoader(conn)
    await loader.load(users=users, plan=plan_of())
    await conn.execute(
        text(
            "UPDATE session_types st SET conferencing_option_id = o.id "
            "FROM mentor_conferencing_options o WHERE o.user_id = st.mentor_user_id"
        )
    )

    await loader.load(
        users=users,
        plan=plan_of(session_types=(SessionTypeRow(MENTOR, "General Mentorship", 45, venue),)),
    )

    pointer = await conn.execute(text("SELECT conferencing_option_id FROM session_types"))
    assert pointer.scalar_one() is None


@pytest.mark.parametrize(
    "imported",
    [
        "DELETE FROM mentor_conferencing_options",
        "UPDATE mentor_conferencing_options SET is_default = false",
    ],
    ids=["no-daily-row", "daily-demoted"],
)
async def test_a_reload_keeps_the_default_a_mentor_switched_to(
    seeded: tuple[AsyncConnection, dict[str, UUID]], imported: str
) -> None:
    """A mentor who chose Meet keeps it as the default when a reload plans
    `daily`, whether their imported `daily` row is gone or demoted. Neither the
    insert nor the conflict update may claim a second default, which the
    one-default index would refuse, aborting the whole load."""
    conn, users = seeded
    loader = SessionLoader(conn)
    await loader.load(users=users, plan=plan_of())
    await conn.execute(text(imported))
    await conn.execute(
        text(
            "INSERT INTO mentor_conferencing_options (user_id, provider, is_default, source) "
            "VALUES (:u, 'google_meet', true, 'mentor')"
        ),
        {"u": users[MENTOR]},
    )

    await loader.load(users=users, plan=plan_of())

    rows = await conn.execute(
        text(
            "SELECT provider, is_default, source FROM mentor_conferencing_options ORDER BY provider"
        )
    )
    assert [tuple(r) for r in rows] == [
        ("daily", False, "import"),
        ("google_meet", True, "mentor"),
    ]


async def test_a_reload_with_no_other_default_makes_the_imported_option_default(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """An imported option left non-default with no default beside it becomes the
    default again, so the unpinned offering resolves to the mentor's venue and
    not to the platform fallback."""
    conn, users = seeded
    loader = SessionLoader(conn)
    await loader.load(users=users, plan=plan_of())
    await conn.execute(text("UPDATE mentor_conferencing_options SET is_default = false"))

    await loader.load(users=users, plan=plan_of())

    default = await conn.execute(
        text("SELECT provider FROM mentor_conferencing_options WHERE is_default")
    )
    assert default.scalar_one() == "daily"


async def test_a_reload_never_takes_back_a_choice_the_mentor_made(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """The load's upsert on `(user_id, provider)` must not rewrite `source`: a
    mentor who set Meet through `/me/conferencing` keeps `mentor`, so the cleanup
    of imported rows can never remove it after a reload."""
    conn, users = seeded
    mentor = users[MENTOR]
    await conn.execute(
        text(
            "INSERT INTO mentor_conferencing_options (user_id, provider, is_default, source) "
            "VALUES (:u, 'google_meet', true, 'mentor')"
        ),
        {"u": mentor},
    )

    await conn.execute(
        text(UPSERT_CONFERENCING_OPTION), {"user_id": mentor, "provider": "google_meet"}
    )

    source = await conn.execute(
        text("SELECT source FROM mentor_conferencing_options WHERE user_id = :u"), {"u": mentor}
    )
    assert source.scalar_one() == "mentor"


async def test_the_load_leaves_every_offering_inheriting_its_confirmation(
    seeded: tuple[AsyncConnection, dict[str, UUID]],
) -> None:
    """**Null, not the mentor's value — and the difference is the whole reversal.**

    `requires_booking_confirmation` went back to `mentor_profiles`, where the
    profile load writes it. Null on the config means *inherit from the mentor*.

    Writing the mentor's own value down here instead would look harmless and
    reconcile perfectly: every offering would hold exactly what the mentor chose,
    every count would match, and every existing assertion would pass. It would
    also make every migrated offering a permanent **override** that merely
    happens to agree — so the mentor's toggle would change `mentor_profiles`,
    change nothing anybody resolves, and the setting would appear not to work.

    That is the defect this migration exists to remove, arriving through the ETL
    instead of through the schema. It is asserted here because nothing else would
    catch it: the value would be right.
    """
    conn, users = seeded

    await SessionLoader(conn).load(users=users, plan=plan_of())

    row = (
        await conn.execute(
            text("SELECT requires_booking_confirmation FROM session_type_booking_configs")
        )
    ).one()
    assert row.requires_booking_confirmation is None


# `test_the_load_names_a_primary_offering` and
# `test_a_re_run_does_not_move_a_primary_somebody_chose` were deleted with
# `primary_session_type_id` in `c8f1a3e2b904`. The first pinned the three-step
# write order the table cycle forced — profile, offering, pointer; the second
# pinned the `IS NULL` that stopped a re-run reassigning a primary the mentor had
# chosen, which every count-based idempotency test above would have missed.
#
# What would bring them back is a column on `mentor_profiles` referencing
# `session_types` again. The cycle, and the write order it dictates, is a
# property of that reference rather than of anything the loader does.

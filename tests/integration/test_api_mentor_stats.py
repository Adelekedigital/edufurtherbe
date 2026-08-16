"""What a mentor has actually done, on the public profile.

Four figures over two tables, and the fixtures here are built to be
*distinguishable* — one session of one minute with one mentee satisfies every
wrong formula equally well.

**A note on what the export actually holds**, because an earlier version of this
file said the opposite. Counting `sessionStatus` on *bookings* finds one
`completed` row and suggests fixtures are the only way to test this. Loading the
export tells a different story: **36 completed sessions across 4 mentors**,
because sessions are merged from bookings *and* the 164 orphan trackers. The
population you measure has to be the one the query reads.

The two that need care are the ones where a status means "we do not know":
`pending` attendance, and a session with no mentor participant row at all.
Neither is absence, and either one entering a denominator reports a mentor as
unreliable for something that never happened.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/mentors"


async def profile(client: httpx.AsyncClient, mentor: UUID) -> dict:
    response = await client.get(f"{URL}/{mentor}")
    assert response.status_code == 200, response.text
    return response.json()


async def add_session(
    engine: AsyncEngine,
    mentor: UUID,
    *,
    status: str = "completed",
    minutes: int = 45,
    mentee: UUID | None = None,
    attendance: str | None = "attended",
    days_ago: int = 1,
    participant: bool = True,
    #: The mentee's own attendance row. Real sessions have two participants,
    #: and a fixture with only the mentor's cannot tell a query scoped to the
    #: mentor from one that reads whichever row it finds first.
    mentee_attendance: str | None = None,
) -> UUID:
    """One session, with every axis a stat reads as a knob.

    `participant=False` leaves the mentor with no `session_participants` row —
    the state of two of the 105 dev bookings, which have no tracker and so carry
    no attendance fact at all.
    """
    async with engine.begin() as conn:
        if mentee is None:
            mentee = (
                await conn.execute(
                    text(
                        "INSERT INTO users (email, first_name, primary_role, timezone) "
                        "VALUES (:e, 'Mentee', 'mentee', 'UTC') RETURNING id"
                    ),
                    {"e": f"mentee-{uuid4()}@example.test"},
                )
            ).scalar_one()
        session_type = (
            await conn.execute(
                text("SELECT id FROM session_types WHERE mentor_user_id = :m LIMIT 1"),
                {"m": mentor},
            )
        ).scalar_one_or_none()
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(mentor_id, mentee_id, session_type_id, starts_at, duration_minutes, status) "
                    "VALUES (:m, :e, :t, :s, :d, CAST(:st AS session_status)) RETURNING id"
                ),
                {
                    "m": mentor,
                    "e": mentee,
                    "t": session_type,
                    "s": dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
                    "d": minutes,
                    "st": status,
                },
            )
        ).scalar_one()
        if participant:
            await conn.execute(
                text(
                    "INSERT INTO session_participants "
                    "(session_id, user_id, role, attendance_status) "
                    "VALUES (:s, :u, 'mentor', CAST(:a AS attendance_status))"
                ),
                {"s": session_id, "u": mentor, "a": attendance},
            )
        if mentee_attendance is not None:
            await conn.execute(
                text(
                    "INSERT INTO session_participants "
                    "(session_id, user_id, role, attendance_status) "
                    "VALUES (:s, :u, 'mentee', CAST(:a AS attendance_status))"
                ),
                {"s": session_id, "u": mentee, "a": mentee_attendance},
            )
        return session_id


# --------------------------------------------------------------------------
# The three counts
# --------------------------------------------------------------------------


async def test_the_four_stats_are_reported(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Deliberately not 1/1/1: three sessions of *different* durations across two
    mentees, so a formula that counted rows, or summed the wrong column, or
    forgot `DISTINCT`, produces a different number from the right one."""
    mentor = await make_bookable_mentor(db_engine, "stats-basic")
    shared = None
    for index, minutes in enumerate((30, 45, 60)):
        session_id = await add_session(db_engine, mentor, minutes=minutes, days_ago=index + 1)
        if shared is None:
            async with db_engine.begin() as conn:
                shared = (
                    await conn.execute(
                        text("SELECT mentee_id FROM sessions WHERE id = :s"), {"s": session_id}
                    )
                ).scalar_one()
    # A fourth session with a mentee who already appeared, so DISTINCT matters.
    await add_session(db_engine, mentor, minutes=15, mentee=shared, days_ago=4)

    body = await profile(api_client, mentor)

    assert body["completed_sessions"] == 4
    assert body["mentoring_minutes"] == 150
    assert body["mentees_mentored"] == 3
    assert body["attendance_rate"] == 100


@pytest.mark.parametrize("status", ["cancelled", "declined", "expired", "confirmed"])
async def test_only_delivered_sessions_are_counted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    mentor = await make_bookable_mentor(db_engine, f"stats-{status}")
    await add_session(db_engine, mentor, status=status, minutes=90)

    body = await profile(api_client, mentor)

    assert body["completed_sessions"] == 0
    assert body["mentoring_minutes"] == 0
    assert body["mentees_mentored"] == 0


async def test_a_mentor_with_nothing_gets_zeros_and_a_null_rate(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Zero would say "never shows up"; null says "no data yet"."""
    mentor = await make_bookable_mentor(db_engine, "stats-empty")

    body = await profile(api_client, mentor)

    assert body["completed_sessions"] == 0
    assert body["mentoring_minutes"] == 0
    assert body["mentees_mentored"] == 0
    assert body["attendance_rate"] is None


async def test_sessions_the_mentor_attended_as_a_mentee_do_not_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor is also somebody's mentee, and a session they received is not one
    they gave. The discovery card has this exact inversion test."""
    mentor = await make_bookable_mentor(db_engine, "stats-side")
    await add_session(db_engine, mentor, minutes=60)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET mentee_id = mentor_id, mentor_id = mentee_id"))

    body = await profile(api_client, mentor)

    assert body["completed_sessions"] == 0
    assert body["mentoring_minutes"] == 0


# --------------------------------------------------------------------------
# Attendance, and the two states that mean "unknown"
# --------------------------------------------------------------------------


async def test_attendance_is_attended_over_expected(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Three of four, so 75% — the design's own figure, and a ratio no rounding
    accident produces from a smaller fixture."""
    mentor = await make_bookable_mentor(db_engine, "stats-rate")
    for index in range(3):
        await add_session(db_engine, mentor, attendance="attended", days_ago=index + 1)
    await add_session(db_engine, mentor, status="no_show", attendance="no_show", days_ago=4)

    body = await profile(api_client, mentor)

    assert body["attendance_rate"] == 75
    assert body["completed_sessions"] == 3, "a no-show is not a delivered session"


async def test_a_pending_participant_moves_neither_half(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`pending` is *unknown*, not absent.

    Its own docstring says so — "we do not know yet is a real answer and
    distinguishable from `NO_SHOW`" — and until the lifecycle that marks
    attendance exists, it is the state of every session booked after cutover.
    Counted as absence, it would report every new mentor as unreliable.

    One attended and one pending is 100%, not 50%.
    """
    mentor = await make_bookable_mentor(db_engine, "stats-pending")
    await add_session(db_engine, mentor, attendance="attended", days_ago=1)
    await add_session(db_engine, mentor, attendance="pending", days_ago=2)

    body = await profile(api_client, mentor)

    assert body["attendance_rate"] == 100


async def test_a_session_with_no_participant_row_moves_neither_half(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two of the 105 dev bookings have no tracker, so no attendance fact exists.

    Absent data is not absence. The mentor here attended their one recorded
    session and should read 100%, not 50%.
    """
    mentor = await make_bookable_mentor(db_engine, "stats-noparticipant")
    await add_session(db_engine, mentor, attendance="attended", days_ago=1)
    await add_session(db_engine, mentor, participant=False, days_ago=2)

    body = await profile(api_client, mentor)

    assert body["attendance_rate"] == 100


async def test_only_terminal_sessions_reach_the_denominator(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A cancelled session is not a missed one, and a confirmed one next week has
    not happened. Either in the denominator reports absence that never occurred.
    """
    mentor = await make_bookable_mentor(db_engine, "stats-terminal")
    await add_session(db_engine, mentor, attendance="attended", days_ago=1)
    await add_session(db_engine, mentor, status="cancelled", attendance="no_show", days_ago=2)
    await add_session(db_engine, mentor, status="confirmed", attendance="pending", days_ago=-7)

    body = await profile(api_client, mentor)

    assert body["attendance_rate"] == 100


async def test_a_no_show_counts_against_the_rate_but_not_the_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The two halves of the design: `no_show` is in the denominator only.

    It held the mentor's time and delivered nothing, so it is not a completed
    session — and it *is* a session they were expected at.
    """
    mentor = await make_bookable_mentor(db_engine, "stats-noshow")
    await add_session(db_engine, mentor, status="no_show", attendance="no_show", days_ago=1)

    body = await profile(api_client, mentor)

    assert body["completed_sessions"] == 0
    assert body["mentoring_minutes"] == 0
    assert body["attendance_rate"] == 0, "expected once, attended never"


# --------------------------------------------------------------------------
# One definition, two readers
# --------------------------------------------------------------------------


async def test_the_card_and_the_profile_report_the_same_completed_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The guard on the extraction.

    `delivered()` exists so "what counts as a session this mentor gave" is one
    predicate. This is what fails if somebody re-inlines it into either caller
    and the two definitions drift.
    """
    mentor = await make_bookable_mentor(db_engine, "stats-agree")
    for index in range(2):
        await add_session(db_engine, mentor, days_ago=index + 1)
    await add_session(db_engine, mentor, status="cancelled", days_ago=3)

    card = (await api_client.get(URL)).json()["data"][0]
    body = await profile(api_client, mentor)

    assert card["completed_sessions"] == body["completed_sessions"] == 2


async def test_a_completed_session_whose_mentor_was_absent_is_defined_behaviour(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The invariant is a convention, so this is what happens when it breaks.

    `completed` is supposed to mean both parties turned up, and across all 103
    linked pairs in the dev export it does. **Nothing enforces it** — status
    arrives from legacy `sessionStatus`, attendance from a separate `TrackStatus`,
    and a `CHECK` cannot tie two tables. So a row like this one is constructible
    today and will be constructible until the session lifecycle derives status
    from attendance.

    It must not crash, and it must not silently pick one source over the other.
    Each stat answers from the column it is about: the session says delivered, so
    it counts; the participant says absent, so the rate falls. A reader seeing
    "1 session, 0% attendance" is looking at exactly the contradiction that
    exists in the data, which is better than a number that hides it.
    """
    mentor = await make_bookable_mentor(db_engine, "stats-contradiction")
    await add_session(db_engine, mentor, status="completed", attendance="no_show", days_ago=1)

    body = await profile(api_client, mentor)

    assert body["completed_sessions"] == 1
    assert body["mentoring_minutes"] == 45
    assert body["attendance_rate"] == 0


async def test_the_mentee_s_absence_is_not_the_mentor_s(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The scope on `user_id`, which nothing else in this file could see.

    Every other fixture here creates only the mentor's participant row, so a
    query reading *whichever* row it found returned the same answer and a
    mutation removing the scope survived. Two rows with opposite statuses is the
    whole test.

    It is also the common real case rather than an edge: the dev export holds ten
    `missed` sessions, and a mentee failing to appear must not mark the mentor
    absent from a session they turned up to.
    """
    mentor = await make_bookable_mentor(db_engine, "stats-menteeabsent")
    await add_session(
        db_engine, mentor, attendance="attended", mentee_attendance="no_show", days_ago=1
    )

    body = await profile(api_client, mentor)

    assert body["attendance_rate"] == 100, "the mentee's absence was read as the mentor's"

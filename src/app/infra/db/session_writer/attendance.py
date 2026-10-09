"""Who turned up: recording an arrival, and settling each session's outcome
once its join window has closed, which is also when reviews are requested.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, insert, or_, select, text, true, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, JoinWindowClosedError, NotFoundError
from app.domain.attendance import (
    DOOR_STATUSES,
    JOIN_CLOSES,
    PRESENCE_REPORTING,
    AttendanceEvidence,
    absent_party,
    door_window,
    join_closes_at,
    join_window,
    presence_decides,
    waits_for_records,
    within_door_window,
    within_join_window,
)
from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    SessionReasonCode,
    SessionRole,
    SessionStatus,
)
from app.domain.notifications import (
    Notification,
)
from app.domain.refunds import no_show_refund
from app.infra.clients.daily_presence import Sighting
from app.infra.clients.meetings import VenueUnavailableError
from app.infra.db.credit_writer import refund_credit
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
    SessionParticipant,
)
from app.infra.db.outbox import enqueue
from app.infra.db.review_eligibility import within_interval
from app.infra.db.session_store import is_a_party

logger = logging.getLogger(__name__)

#: How long the settlement may spend reading Daily's meeting records in one run
#: (Codex on #393). Reads are serial and each may take the client's full
#: timeout, so past this the remaining sessions wait for the next run rather
#: than running the job past its own limit, where nothing would commit. A test
#: holds this plus one client timeout to half the schedule's timeout.
RECORDS_READ_BUDGET = dt.timedelta(seconds=45)


async def _venue_row(session: AsyncSession, session_id: UUID, actor_id: UUID) -> dict[str, Any]:
    """As much of the caller's own session as a way into it needs.

    **One read for the arrival and the door**, which is the point of it being
    here rather than inside either (non-negotiable #8): both need the same row,
    scoped the same way, and a second copy of this `SELECT` is where the two
    would drift — one of them gaining a column or losing the party scope.

    Scoped by `is_a_party` in the query, so a stranger matches no row rather
    than a row that is then refused; :class:`NotFoundError` either way, and
    indistinguishable from a session that does not exist.
    """
    row = (
        (
            await session.execute(
                select(
                    Session.status,
                    Session.starts_at,
                    Session.duration_minutes,
                    Session.meeting_url,
                    Session.meeting_provider,
                    Session.external_room_id,
                    (Session.mentor_id == actor_id).label("is_mentor"),
                    # The caller's own press, for the door's late rule.
                    select(SessionParticipant.joined_at)
                    .where(
                        SessionParticipant.session_id == Session.id,
                        SessionParticipant.user_id == actor_id,
                    )
                    .correlate(Session)
                    .scalar_subquery()
                    .label("caller_joined_at"),
                ).where(
                    Session.id == session_id,
                    is_a_party(actor_id),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError("no such session")
    return dict(row)


def _require_status(row: dict[str, Any], allowed: frozenset[SessionStatus], *, to: str) -> None:
    """Refuse a session whose status is not in `allowed`, saying what was refused.

    **One check, two different sets**, and the difference is the point. An
    arrival needs `confirmed`: once the session is settled its outcome is
    decided, and an arrival recorded afterwards would change it. A door needs
    only that the session was agreed to and not called off — see
    `DOOR_STATUSES` — because settlement closes the outcome, not the room.

    `409` rather than a quiet no-op: a client that believes it succeeded will
    not try again.
    """
    if SessionStatus(row["status"]) not in allowed:
        raise ConflictError(f"a {row['status']} session cannot be {to}")


async def door_row(
    session: AsyncSession,
    session_id: UUID,
    actor_id: UUID,
    *,
    now: dt.datetime,
    opens_before: dt.timedelta,
) -> dict[str, Any]:
    """The caller's way into a running session, **recording nothing** (#379).

    The arrival's twin with two differences, both deliberate. Its window runs
    to the session's **end** rather than fifteen minutes past the start —
    see :func:`door_window` — and it writes no attendance, so it cannot change
    `joined_at`, `attendance_status`, or an outcome already settled.

    Read-only, so it does not need the caller to commit; nothing is pending.
    """
    row = await _venue_row(session, session_id, actor_id)
    _require_status(row, DOOR_STATUSES, to="entered")
    if not within_door_window(
        row["starts_at"], row["duration_minutes"], now, opens_before=opens_before
    ):
        opens, closes = door_window(
            row["starts_at"], row["duration_minutes"], opens_before=opens_before
        )
        raise ConflictError(
            f"this session's room is open between {opens.isoformat()} and {closes.isoformat()}"
        )
    # **No late first-timers** (owner, 2026-10-08). Once arrivals stop, the door
    # is for coming back, so only a party who pressed Join in time is let in.
    # The frontend already offers Rejoin only then; this closes the direct call.
    late = now >= join_closes_at(row["starts_at"], int(row["duration_minutes"]))
    if late and row["caller_joined_at"] is None:
        raise JoinWindowClosedError("arrivals have closed and you did not join in time")
    return row


async def record_arrival(
    session: AsyncSession,
    session_id: UUID,
    actor_id: UUID,
    *,
    now: dt.datetime,
    opens_before: dt.timedelta,
) -> dict[str, Any]:
    """Mark the caller present at their own session.

    **Only their own row.** The `WHERE` names `user_id = actor_id`, so a mentor
    cannot mark a mentee present or a mentee vouch for a mentor — which matters
    because attendance drives both parties' reliability statistics, so marking
    somebody else present is editing their record.

    **Idempotent, and `joined_at` keeps the *first* arrival.** Pressing Join
    twice is the ordinary case — a dropped call, a refreshed tab — and the
    second press must not rewrite when they arrived. The `COALESCE` is what does
    that; without it the column would record the last press, which is the one
    fact nobody wants.

    Returns what the caller needs to be sent somewhere: the venue, the stored
    URL, the room's provider-side name, and which side of the session they are
    on. **Resolving that into a door is the caller's job**, not this one's — the
    writer has no HTTP client and should not grow one.

    Raises :class:`NotFoundError` when the session is not the caller's, and
    :class:`ConflictError` when it is not confirmed or the window is shut. Does
    not commit.
    """
    row = await _venue_row(session, session_id, actor_id)
    _require_status(row, frozenset({SessionStatus.CONFIRMED}), to="joined")
    length = int(row["duration_minutes"])
    if not within_join_window(row["starts_at"], length, now, opens_before=opens_before):
        opens, closes = join_window(row["starts_at"], length, opens_before=opens_before)
        raise ConflictError(
            f"this session can be joined between {opens.isoformat()} and {closes.isoformat()}"
        )

    recorded = await session.execute(
        update(SessionParticipant)
        .where(
            SessionParticipant.session_id == session_id,
            SessionParticipant.user_id == actor_id,
        )
        .values(
            joined_at=func.coalesce(SessionParticipant.joined_at, now),
            # **The press is the evidence only where nothing saw the room**
            # (#382). For a Daily session it is the way in, and attendance waits
            # for Daily to report the party present (`observe_presence`).
            **(
                {}
                if presence_decides(row["meeting_provider"], has_room=bool(row["external_room_id"]))
                else {"attendance_status": AttendanceStatus.ATTENDED}
            ),
        )
    )
    # `rowcount` is a `CursorResult` attribute and `execute()` is typed as the
    # `Result` base, so the cast is a typing fact rather than a runtime one:
    # a DML statement always returns a cursor result.
    if not cast("CursorResult[Any]", recorded).rowcount:
        # **A silent success is the worst answer available here.** Every session
        # booked through this service has both rows, written in the same
        # transaction, so reaching this means a session that predates that or
        # one whose rows were removed — and the caller would otherwise be told
        # their arrival was recorded, walk away, and be settled as absent with
        # nothing to appeal against.
        raise ConflictError("this session has no attendance record for you")

    return row


def _join_closes_sql() -> Any:
    """When arrivals stop, in SQL: :func:`join_closes_at`'s rule, fifteen
    minutes in or the session's end if sooner. One expression, read by the
    settlement and by a sighting, so the two cannot disagree on the instant."""
    fifteen = text(f"interval '{int(JOIN_CLOSES.total_seconds())} seconds'")
    length = func.make_interval(0, 0, 0, 0, 0, Session.duration_minutes)
    return Session.starts_at + func.least(fifteen, length)


def _presence_decides_sql() -> Any:
    """:func:`presence_decides` in SQL, held to it by a test: a venue that
    reports presence, and a room that exists."""
    return and_(
        Session.meeting_provider.in_([str(p) for p in PRESENCE_REPORTING]),
        Session.external_room_id.is_not(None),
    )


def _not_seen_in_time() -> Any:
    """A participant the provider has not seen in the room before arrivals
    stopped, in SQL, correlated to ``Session``. Read by the records check and by
    the settlement, so the party looked up is the party judged."""
    return or_(
        SessionParticipant.in_room_at.is_(None),
        SessionParticipant.in_room_at >= _join_closes_sql(),
    )


def _window_shut(now: dt.datetime) -> Any:
    """Sessions whose join window has shut, in SQL: fifteen minutes in, or the
    session's end if that is sooner — :func:`join_window`'s rule.

    Built from :data:`JOIN_CLOSES` rather than from a literal, and pinned to the
    domain function by a test across lengths, because a settlement running a
    minute early would mark somebody absent while they still had time to arrive.
    """
    return _join_closes_sql() <= now


async def settle_attendance(
    session: AsyncSession, *, now: dt.datetime, unverified: frozenset[UUID] = frozenset()
) -> int:
    """Decide every confirmed session whose join window has shut. Returns the count.

    **This is the producer `session_stats` has been waiting for.** That module
    records the problem plainly: `sessions.status` and
    `session_participants.attendance_status` are independent columns, a `CHECK`
    cannot tie them because it spans two tables, and *the migrated data already
    violates the invariant* — three sessions are `completed` with somebody
    absent. This settles sessions **booked after cutover** from their attendance,
    so from here forward there is one place the outcome is decided.

    It does not retrofit the migrated rows, and must not: those record what the
    legacy app believed, and rewriting them would destroy the evidence that the
    two figures disagree.

    **Set-based, in three statements plus the log.** The whole population is
    about a thousand sessions, so a per-session loop would buy nothing and cost
    a round trip each — and it would make a partial settlement reachable, where
    a session says `completed` while its participants still say `pending`.

    **Idempotent.** The participant update touches only `pending` rows and the
    session update only `confirmed` ones, so a second run settles nothing and
    writes no second event. That matters more than usual: this is driven by an
    external scheduler, because settled decision #13 rules out a platform-native
    cron — and an external scheduler is the kind that fires twice.

    **``unverified`` waits for the next run** (#382): sessions whose presence
    the meeting records could not confirm, because Daily could not be reached.
    Settling them on silence would brand both parties absent and refund the
    wrong person.

    Does not commit.
    """
    ready = and_(
        Session.status == SessionStatus.CONFIRMED,
        _window_shut(now),
        Session.id.not_in(unverified) if unverified else true(),
    )
    due = select(Session.id).where(ready)

    # 1. Everybody still unknown was absent. `pending` only, so an arrival
    #    already recorded by `record_arrival` is left exactly as it is.
    #
    #    **Except where presence decides** (Codex on #393): there a party
    #    not seen in the room in time is absent *whatever the row says*. A
    #    press recorded by the code before #382, during a rolling deploy,
    #    left `attended` with no sighting; settling on it would call a session
    #    nobody entered `completed`.
    unseen = (
        select(Session.id)
        .where(
            Session.id == SessionParticipant.session_id,
            ready,
            _presence_decides_sql(),
            _not_seen_in_time(),
        )
        .correlate(SessionParticipant)
        .exists()
    )
    await session.execute(
        update(SessionParticipant)
        .where(
            SessionParticipant.session_id.in_(due),
            or_(SessionParticipant.attendance_status == AttendanceStatus.PENDING, unseen),
        )
        .values(attendance_status=AttendanceStatus.NO_SHOW)
    )

    # 2. The session's own outcome: **both named parties recorded present**.
    #
    #    Counted rather than asked as "is anybody absent", which is the same
    #    question in the ordinary case and the wrong one at the edges — a
    #    session with one participant row, or with none, contains nobody absent
    #    and was settled as `completed`. `sessions` is 1:1 between exactly one
    #    mentor and one mentee by design (package D4), so the expected set is
    #    knowable and `= 2` is that invariant rather than a magic number. Group
    #    sessions would change this line, and would change the column layout
    #    that makes it possible.
    #
    #    **This is `domain.attendance.outcome` expressed in SQL**, which is one
    #    rule in two places. The Python is the specification and this is the
    #    implementation, pinned by `test_the_settlement_agrees_with_the_rule` —
    #    which used to drive only sessions created through booking, so it never
    #    reached the case the two disagreed on.
    def _came(party: Any) -> Any:
        return (
            select(SessionParticipant.id)
            .where(
                SessionParticipant.session_id == Session.id,
                SessionParticipant.user_id == party,
                SessionParticipant.attendance_status == AttendanceStatus.ATTENDED,
            )
            .correlate(Session)
            .exists()
        )

    mentor_came = _came(Session.mentor_id)
    mentee_came = _came(Session.mentee_id)
    settled = (
        (
            await session.execute(
                update(Session)
                .where(ready)
                .values(
                    status=case(
                        (
                            and_(mentor_came, mentee_came),
                            SessionStatus.COMPLETED.value,
                        ),
                        else_=SessionStatus.NO_SHOW.value,
                    )
                )
                # The two facts travel with the row so the reason code can name
                # the party who was absent. PostgreSQL allows a subquery in
                # `RETURNING`, and it is evaluated against the *pre-update* row —
                # which is what is wanted: attendance is not what this statement
                # changes.
                .returning(
                    Session.id,
                    Session.status,
                    # Carried out with the outcome so the review request can be
                    # addressed without a second read of rows this statement has
                    # already touched.
                    Session.mentee_id,
                    mentor_came.label("mentor_came"),
                    mentee_came.label("mentee_came"),
                    _presence_decides_sql().label("observed"),
                )
            )
        )
        .mappings()
        .all()
    )
    if not settled:
        return 0

    # 3. The log. `actor_id` is null and `actor_type` is `system`, which the
    #    model's own docstring calls honest for a sweep and better than
    #    inventing a system user. `from_status` is `confirmed` on every row,
    #    because nothing else was selected.
    #
    #    **The evidence is recorded with the outcome**, and this is the first
    #    writer `session_events.metadata` has had. Today every outcome is
    #    `REPORTED` — both parties pressed a button and nothing observed the
    #    room. When a provider starts reporting join and leave, the same field
    #    carries `OBSERVED` and the two become distinguishable *retrospectively*,
    #    which is the whole reason to write it before anything reads it: a
    #    session settled last month cannot be re-examined for how it was judged.
    #
    #    It is what lets a later payout rule require observed attendance without
    #    a second status, and what stops `completed` quietly meaning two
    #    different things either side of the Daily integration.
    await session.execute(
        insert(SessionEvent),
        [
            {
                "session_id": row["id"],
                "from_status": SessionStatus.CONFIRMED,
                "to_status": row["status"],
                "actor_id": None,
                "actor_type": ActorType.SYSTEM,
                "reason_code": _absence_code(
                    mentor_came=bool(row["mentor_came"]), mentee_came=bool(row["mentee_came"])
                ),
                # `metadata_`, not `"metadata"`. The attribute is renamed to
                # dodge `Base.metadata`, and an ORM insert matches attribute
                # names — the column name is accepted in silence and the
                # server default written instead, which a test caught.
                "metadata_": {
                    "evidence": (
                        AttendanceEvidence.OBSERVED
                        if row["observed"]
                        else AttendanceEvidence.REPORTED
                    )
                },
            }
            for row in settled
        ],
    )

    # 4. **A mentor who never came owes the mentee their credit** (decision
    #    229). Decided by `domain.refunds` from the same two facts the reason
    #    code above reads, and paid in the settling transaction, so an outcome
    #    and its refund commit together. Only rows this run settled reach here,
    #    and `refund_credit` is once-per-session at the database besides.
    for row in settled:
        owed = no_show_refund(
            mentor_came=bool(row["mentor_came"]), mentee_came=bool(row["mentee_came"])
        )
        if owed is not None:
            await refund_credit(session, row["mentee_id"], row["id"], reason=owed, now=now)

    await _request_reviews(
        session,
        [row["id"] for row in settled if row["status"] == SessionStatus.COMPLETED],
        now=now,
    )

    return len(settled)


async def _request_reviews(
    session: AsyncSession, completed: list[UUID], *, now: dt.datetime
) -> None:
    """Ask each mentee how the session went, unless the interval says not to.

    **Fired by the transition, which is the whole of decision 10.** A clock set
    to "end plus ten minutes" races this sweep and can ask about a session
    nobody attended; a session that settles as `no_show` never reaches this list
    at all, so the wrong message is unreachable rather than merely unlikely.

    **Suppressed by the predicate the write refuses on**, not by a second copy
    of the rule. `within_interval` already takes its arguments as columns — the
    profile picker passes them that way — so the set-based question here needs
    no reshaping of it. Two rules for "is a review wanted" would eventually
    disagree, and the disagreement is a mentee asked for something the endpoint
    then refuses (#8, and decision 8 by name).

    **One extra statement, not one per session.** The sweep settles about a
    thousand rows in three statements and a log; a per-session suppression check
    would add a round trip each for a question one `WHERE` can answer.

    Does not commit. The enqueue is in the settling transaction, so a message
    about a settlement that rolled back cannot exist.
    """
    if not completed:
        return

    wanted = (
        (
            await session.execute(
                select(Session.id, Session.mentee_id).where(
                    Session.id.in_(completed),
                    ~within_interval(Session.mentee_id, Session.session_type_id, now),
                )
            )
        )
        .mappings()
        .all()
    )

    for row in wanted:
        await enqueue(
            session,
            Notification.REVIEW_REQUESTED,
            entity_type="session",
            entity_id=row["id"],
            recipient_ids=(row["mentee_id"],),
        )


def _absence_code(*, mentor_came: bool, mentee_came: bool) -> SessionReasonCode | None:
    """Which party was absent, when exactly one was.

    **Named when the vocabulary can say it, null when it cannot.** The first
    version returned `MENTEE_NO_SHOW` for every missed session, on the argument
    that the participant rows already hold the per-person truth and a session
    event asserting one party would be a second copy. Half of that argument
    survives and half does not: it is right that *both absent* has no correct
    code, and wrong that a mentor's absence should be filed under the mentee's —
    naming the wrong party is not coarseness, it is the wrong answer. Who missed
    it is :func:`app.domain.attendance.absent_party`, the same call the no-show
    refund makes (decision 229), so the code and the refund cannot disagree.

    So: null when the session happened, null when both were absent — the
    participant rows are the only thing that can state that, and inventing a
    code would make an aggregate over `reason_code` wrong in a way nobody could
    see — and the exact code when exactly one party failed to turn up.
    """
    missed = absent_party(mentor_attended=mentor_came, mentee_attended=mentee_came)
    if missed is SessionRole.MENTOR:
        return SessionReasonCode.MENTOR_NO_SHOW
    if missed is SessionRole.MENTEE:
        return SessionReasonCode.MENTEE_NO_SHOW
    return None


async def observe_presence(session: AsyncSession, sighting: Sighting) -> bool:
    """Record that the provider saw a party in the room. Returns whether a row
    matched. Does not commit.

    **Room and user, together, in one statement** (#382). The room must be the
    session's own Daily room and the user one of its parties, so a sighting for
    anyone else, or for a party of another session, matches no row. Nothing is
    read first and checked after.

    **Order-free and repeatable.** Daily delivers "roughly, but not strictly, in
    order" and may repeat, so ``in_room_at`` only ever moves earlier, and
    replaying a sighting changes nothing.

    **Counted only before arrivals stop.** A sighting after that is kept, so the
    page can show it, but never turns a pending party present: the outcome is
    decided at that instant, as it is for the press.
    """
    try:
        user_id = UUID(sighting.user_id)
    except ValueError:
        # Not one of ours: every token we mint carries a user's uuid.
        return False
    in_time = and_(
        sighting.at < _join_closes_sql(),
        SessionParticipant.attendance_status == AttendanceStatus.PENDING,
        Session.status == SessionStatus.CONFIRMED,
    )
    matched = await session.execute(
        update(SessionParticipant)
        .where(
            SessionParticipant.session_id == Session.id,
            SessionParticipant.user_id == user_id,
            Session.external_room_id == sighting.room,
            _presence_decides_sql(),
        )
        .values(
            in_room_at=func.least(
                func.coalesce(SessionParticipant.in_room_at, sighting.at), sighting.at
            ),
            attendance_status=case(
                (in_time, AttendanceStatus.ATTENDED.value),
                else_=SessionParticipant.attendance_status,
            ),
        )
    )
    return bool(cast("CursorResult[Any]", matched).rowcount)


async def presence_to_confirm(
    session: AsyncSession, *, now: dt.datetime
) -> list[tuple[UUID, str, dt.datetime]]:
    """Sessions to check against Daily's meeting records before settling (#382).

    Due to settle, decided by presence, and with a party still pending, which
    is a party no webhook reported. Each comes with its room and the instant
    arrivals stopped, so the caller can tell how long it has waited.
    """
    pending = (
        select(SessionParticipant.id)
        .where(SessionParticipant.session_id == Session.id, _not_seen_in_time())
        .correlate(Session)
        .exists()
    )
    rows = await session.execute(
        select(Session.id, Session.external_room_id, _join_closes_sql())
        .where(
            Session.status == SessionStatus.CONFIRMED,
            _window_shut(now),
            _presence_decides_sql(),
            pending,
        )
        # Oldest first, so a session the read budget skips is reached on a
        # later run rather than skipped behind newer ones indefinitely.
        .order_by(_join_closes_sql())
    )
    return [(row[0], str(row[1]), row[2]) for row in rows.all()]


async def confirm_presence(
    session: AsyncSession,
    *,
    now: dt.datetime,
    rooms: Any,
    budget: dt.timedelta = RECORDS_READ_BUDGET,
) -> frozenset[UUID]:
    """Check Daily's meeting records for every party no webhook reported, before
    the settlement decides them (#382). Returns the sessions to leave unsettled
    this run. Does not commit.

    **The webhook can go quiet.** Daily stops sending after three failed
    deliveries, and a party it never reported would otherwise be settled absent,
    so each due session with a party still pending is read from the records,
    through the same :func:`observe_presence` a webhook uses.

    **Unreadable records hold the session back**, for up to
    :data:`PRESENCE_RECORDS_PATIENCE`, because silence is not absence. That
    includes a provider that is not configured (``NotImplementedError``): a key
    removed after rooms were made leaves records nobody can read, which is
    unreadable, not empty (Codex on #393).

    The provider's client is blocking, so each read runs in a thread.
    """
    unverified: set[UUID] = set()
    deadline = time.monotonic() + budget.total_seconds()
    for session_id, room, closed_at in await presence_to_confirm(session, now=now):
        if time.monotonic() >= deadline:
            # **Out of time is not unreadable** (Codex on #393): never asked, so
            # it waits for a run that asks, however old. The patience below is
            # for records Daily would not give, not for reads we skipped.
            unverified.add(session_id)
            continue
        try:
            found = await asyncio.to_thread(rooms.sightings, room)
        except (VenueUnavailableError, NotImplementedError) as exc:
            # **Unconfigured waits too** (Codex on #393): a key removed after
            # rooms were made leaves records nobody can read, which is the same
            # as unreachable, not the same as nobody there.
            if waits_for_records(closed_at, now):
                logger.warning(
                    "meeting records unreadable for session %s; waiting: %s", session_id, exc
                )
                unverified.add(session_id)
            else:
                logger.warning(
                    "meeting records unreadable for session %s; settling: %s", session_id, exc
                )
            continue
        for sighting in found:
            await observe_presence(session, sighting)
    return frozenset(unverified)

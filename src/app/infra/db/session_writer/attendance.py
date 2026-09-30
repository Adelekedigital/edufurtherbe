"""Who turned up: recording an arrival, and settling each session's outcome
once its join window has closed, which is also when reviews are requested.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, insert, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.domain.attendance import (
    JOIN_CLOSES,
    AttendanceEvidence,
    join_window,
    within_join_window,
)
from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    SessionReasonCode,
    SessionStatus,
)
from app.domain.notifications import (
    Notification,
)
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
    SessionParticipant,
)
from app.infra.db.outbox import enqueue
from app.infra.db.review_eligibility import within_interval


async def record_arrival(
    session: AsyncSession, session_id: UUID, actor_id: UUID, *, now: dt.datetime
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
                ).where(
                    Session.id == session_id,
                    or_(Session.mentor_id == actor_id, Session.mentee_id == actor_id),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError("no such session")
    if SessionStatus(row["status"]) is not SessionStatus.CONFIRMED:
        # A pending request has not been agreed to and a terminal one is over.
        # Both are `409` rather than a quiet no-op: a client that believes it has
        # registered an arrival will not try again.
        raise ConflictError(f"a {row['status']} session cannot be joined")
    if not within_join_window(row["starts_at"], now):
        opens, closes = join_window(row["starts_at"])
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
            attendance_status=AttendanceStatus.ATTENDED,
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

    return dict(row)


def _window_shut(now: dt.datetime) -> Any:
    """Sessions whose join window has shut, in SQL.

    Built from :data:`JOIN_CLOSES` rather than from a literal, so this boundary
    and :func:`window_has_closed`'s are one definition rather than two that
    happen to agree today. A settlement running a minute early would mark
    somebody absent while they still had time to arrive.
    """
    shut = text(f"interval '{int(JOIN_CLOSES.total_seconds())} seconds'")
    return Session.starts_at + shut <= now


async def settle_attendance(session: AsyncSession, *, now: dt.datetime) -> int:
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

    Does not commit.
    """
    due = select(Session.id).where(Session.status == SessionStatus.CONFIRMED, _window_shut(now))

    # 1. Everybody still unknown was absent. `pending` only, so an arrival
    #    already recorded by `record_arrival` is left exactly as it is.
    await session.execute(
        update(SessionParticipant)
        .where(
            SessionParticipant.session_id.in_(due),
            SessionParticipant.attendance_status == AttendanceStatus.PENDING,
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
                .where(Session.status == SessionStatus.CONFIRMED, _window_shut(now))
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
                "metadata_": {"evidence": AttendanceEvidence.REPORTED},
            }
            for row in settled
        ],
    )

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
    these codes are what refund policy runs on, so naming the wrong party is
    not coarseness, it is the wrong answer to the question that decides a
    refund.

    So: null when the session happened, null when both were absent — the
    participant rows are the only thing that can state that, and inventing a
    code would make an aggregate over `reason_code` wrong in a way nobody could
    see — and the exact code when exactly one party failed to turn up.
    """
    if mentor_came and mentee_came:
        return None
    if mentee_came:
        return SessionReasonCode.MENTOR_NO_SHOW
    if mentor_came:
        return SessionReasonCode.MENTEE_NO_SHOW
    return None

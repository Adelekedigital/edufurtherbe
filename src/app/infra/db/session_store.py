"""Reading sessions, and the events that record what happened to them.

**Every statement carries the viewer**, in the ``WHERE`` clause rather than in a
check the caller makes afterwards. A hidden row is not a protected row: this
project has already shipped a list endpoint that scoped correctly beside an
action endpoint that reached other owners' rows by id, and that is the failure
this module is shaped to prevent.

``session_events`` has **no party columns of its own** — no ``mentor_id``, no
``mentee_id``. It is reachable only through the session it belongs to, so its
scoping is a *join*, not a predicate on the table itself. That is the one shape
here a reader might not expect, and getting it wrong reads as "scoped" while
returning any session's history to anyone who knows an id.

There is no ``deleted_at`` predicate anywhere, and that is deliberate rather than
an omission. ``sessions`` has no soft delete: a cancelled session is still a
session, still counted, still part of both parties' history (project vocabulary).

**One derived figure travels with a session: the mentee's attendance rate.** It
is here rather than on a mentee endpoint of its own because the question it
answers is asked *on the request card* — a mentor deciding whether to accept —
and a second round trip per card is worse for the same work. The arithmetic
belongs to ``session_stats`` and is imported, not restated.

**Each party's arrival travels with it too**, for the same reason and a second
one: the screen that needs it is the one a participant is sitting on while they
wait, and *"your mentor has not joined yet"* is a different message from *"your
mentor left"*. A client cannot tell those apart from the session alone.

**So does a preview of the intake answers**: how many, and the first, so a
list of bookings can show what each mentee wants to cover without a request
per row. It is one extra query per page, read through the same rows and fold
as ``GET /sessions/{id}/answers`` (``session_answer_rows``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, case, func, literal, or_, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ValidationError
from app.infra.db.holds import active_hold
from app.infra.db.models.sessions import Session, SessionEvent, SessionParticipant, SessionType
from app.infra.db.models.suggestions import SessionSuggestion
from app.infra.db.models.user import User, UserProfile
from app.infra.db.predicates import live
from app.infra.db.qualifications import top_qualification
from app.infra.db.session_answer_rows import answer_previews
from app.infra.db.session_stats import MENTEE, attendance_rate, attendance_sessions

__all__ = ["get_session", "is_a_party", "list_session_events", "list_sessions"]

#: What both parties may read about a session. `created_by` is absent — it is
#: null on every migrated row and is an internal attribution rather than
#: something either party needs.
_SESSION_COLUMNS = (
    Session.id,
    Session.mentor_id,
    Session.mentee_id,
    Session.session_type_id,
    # The offering's **current** name, whatever its state (#185): retiring an
    # offering does not change what an old session was about.
    SessionType.name.label("session_type_name"),
    Session.status,
    Session.starts_at,
    Session.duration_minutes,
    Session.topic,
    Session.booking_message,
    Session.meeting_provider,
    Session.meeting_url,
    Session.respond_by,
    Session.created_at,
    # **The mentee's reliability, on the row where a mentor decides.**
    #
    # Correlated on `Session.mentee_id` rather than fetched per row, so a page
    # of sessions is still one statement. `session_stats` owns the arithmetic
    # and the side is passed in — the mentor's public rate is the same function
    # with `MENTOR`, and pooling the two would let a diligent mentee's record
    # flatter an unreliable mentor.
    #
    # Returned to **both** parties. It is the mentee's own data, and a mentor
    # about to accept a request is precisely who it is for. There is no matching
    # mentor rate here: that one is already on the public profile, and adding a
    # second copy scoped differently is how a number acquires two definitions.
    attendance_rate(Session.mentee_id, MENTEE).label("mentee_attendance_rate"),
    # Its denominator, from the same base: the "(12 sessions)" beside it.
    attendance_sessions(Session.mentee_id, MENTEE).label("mentee_attendance_sessions"),
    # **Another time the mentor offered** when ending this session (#339). Its
    # status is read from `holds.active_hold`, the rule the grid and booking
    # obey, so "active" here is exactly "still held" there.
    SessionSuggestion.id.label("suggestion_id"),
    SessionSuggestion.starts_at.label("suggestion_starts_at"),
    SessionSuggestion.duration_minutes.label("suggestion_duration_minutes"),
    SessionSuggestion.held_until.label("suggestion_held_until"),
    SessionSuggestion.accepted_session_id.label("suggestion_booked_session_id"),
    case(
        (SessionSuggestion.accepted_session_id.is_not(None), literal("booked")),
        (and_(*active_hold(func.now())), literal("active")),
        else_=literal("expired"),
    ).label("suggestion_status"),
)

#: The two people, aliased per side so one statement can join `users` twice.
#:
#: **`users` is joined INNER and `user_profiles` OUTER, and the asymmetry is the
#: point.** `sessions.mentor_id` is `NOT NULL` with a foreign key, so the user row
#: is guaranteed to exist and an outer join there would be a guard nothing can
#: reach — the shape this repository has already recorded as untestable. Nothing
#: guarantees a `user_profiles` row: a user who never filled in a profile has
#: none, and an inner join would make their sessions vanish from *both* parties'
#: lists, which is a data-loss-shaped bug wearing a display bug's clothes.
#:
#: **A party who deleted their account keeps their place and loses their name**
#: (owner, 2026-09-29; settled decision #93, amended). The session is still the
#: authorization — both parties keep reading it — but the people are joined
#: through `live()`, *outer*, exactly as `with_author` joins a review's author:
#: the row stays, its ids stay, and the leaver's name and avatar come back null
#: with `{side}_deleted` saying why. Before this, a review's session link named
#: a reviewer who had deleted their account (#285's review).
_MENTOR = aliased(User, name="mentor_user")
_MENTEE = aliased(User, name="mentee_user")
_MENTOR_PROFILE = aliased(UserProfile, name="mentor_profile")
_MENTEE_PROFILE = aliased(UserProfile, name="mentee_profile")
_MENTOR_QUALIFICATION = top_qualification(_MENTOR.id, name="mentor_qualification")
_MENTEE_QUALIFICATION = top_qualification(_MENTEE.id, name="mentee_qualification")

#: Each party's attendance, correlated per side.
#:
#: **Correlated subqueries rather than two more joins**, and the reason is the
#: page. `list_sessions` is keyset-paged, and a join to `session_participants`
#: would multiply rows before the limit is applied — which on a paged list does
#: not merely repeat a card, it consumes the page and shifts the cursor, so rows
#: are lost at the boundary rather than seen twice. `mentor_is_bookable` records
#: the same trap from the other direction.
#:
#: A session with no participant row for a side reports `null` and `pending`,
#: which is what two of the 105 dev bookings look like — and the honest answer,
#: since a missing row is not an arrival.


def _attendance(side: Any, column: Any, label: str) -> Any:
    return (
        select(column)
        .where(
            SessionParticipant.session_id == Session.id,
            SessionParticipant.user_id == side,
        )
        .correlate(Session)
        .scalar_subquery()
        .label(label)
    )


_PARTY_COLUMNS = (
    # `mentor_id` is NOT NULL with a foreign key, so the only way the outer
    # join finds no user is `live()` refusing one.
    _MENTOR.id.is_(None).label("mentor_deleted"),
    _MENTEE.id.is_(None).label("mentee_deleted"),
    _MENTOR.first_name.label("mentor_first_name"),
    _MENTOR.last_name.label("mentor_last_name"),
    _MENTOR_PROFILE.avatar_url.label("mentor_avatar_url"),
    _MENTOR_PROFILE.avatar_focus_x.label("mentor_avatar_focus_x"),
    _MENTOR_PROFILE.avatar_focus_y.label("mentor_avatar_focus_y"),
    # Through the same `live()` join as the name, so a party who left takes
    # their zone with them.
    _MENTOR.timezone.label("mentor_timezone"),
    _MENTEE.first_name.label("mentee_first_name"),
    _MENTEE.last_name.label("mentee_last_name"),
    _MENTEE_PROFILE.avatar_url.label("mentee_avatar_url"),
    _MENTEE_PROFILE.avatar_focus_x.label("mentee_avatar_focus_x"),
    _MENTEE_PROFILE.avatar_focus_y.label("mentee_avatar_focus_y"),
    _MENTEE.timezone.label("mentee_timezone"),
    # Each party's top qualification, the pending card's "BSc Student at
    # FUTA". Correlated on the **live** alias, so a party who left loses it
    # with their name.
    _MENTOR_QUALIFICATION.c.degree.label("mentor_degree"),
    _MENTOR_QUALIFICATION.c.institution.label("mentor_institution"),
    _MENTEE_QUALIFICATION.c.degree.label("mentee_degree"),
    _MENTEE_QUALIFICATION.c.institution.label("mentee_institution"),
    _attendance(Session.mentor_id, SessionParticipant.joined_at, "mentor_joined_at"),
    _attendance(Session.mentor_id, SessionParticipant.in_room_at, "mentor_in_room_at"),
    _attendance(
        Session.mentor_id, SessionParticipant.attendance_status, "mentor_attendance_status"
    ),
    _attendance(Session.mentee_id, SessionParticipant.joined_at, "mentee_joined_at"),
    _attendance(Session.mentee_id, SessionParticipant.in_room_at, "mentee_in_room_at"),
    _attendance(
        Session.mentee_id, SessionParticipant.attendance_status, "mentee_attendance_status"
    ),
)


def _with_parties(statement: Select[Any]) -> Select[Any]:
    """Attach both people to a session query.

    Written once and applied by both readers, so a client cannot get a named
    mentor from the list and a bare id from the detail. Every join is many-to-one
    on a primary key or a unique `user_id`, so no statement gains a row — which
    matters because the list is keyset-paged and a duplicate would corrupt the
    page rather than merely repeat a name.

    **Outer, through `live()`**, so a deleted party's session is still listed
    and only their identity goes. The profile is keyed on the *joined* user,
    not on the session's id column: keyed on the session, the avatar of someone
    who left would still come back beside their nulled name.
    """
    return (
        statement.select_from(Session)
        .outerjoin(_MENTOR, and_(_MENTOR.id == Session.mentor_id, live(_MENTOR)))
        .outerjoin(_MENTEE, and_(_MENTEE.id == Session.mentee_id, live(_MENTEE)))
        .outerjoin(_MENTOR_PROFILE, _MENTOR_PROFILE.user_id == _MENTOR.id)
        .outerjoin(_MENTEE_PROFILE, _MENTEE_PROFILE.user_id == _MENTEE.id)
        # One row each by construction (`limit 1`), so a page is not multiplied.
        .outerjoin(_MENTOR_QUALIFICATION, true())
        .outerjoin(_MENTEE_QUALIFICATION, true())
        # On its primary key and unfiltered, so no row is gained or lost.
        .outerjoin(SessionType, SessionType.id == Session.session_type_id)
        # Unique on `session_id`, so at most one row joins and the page holds.
        .outerjoin(SessionSuggestion, SessionSuggestion.session_id == Session.id)
    )


_EVENT_COLUMNS = (
    SessionEvent.id,
    SessionEvent.from_status,
    SessionEvent.to_status,
    SessionEvent.actor_id,
    SessionEvent.actor_type,
    SessionEvent.reason_code,
    SessionEvent.reason_text,
    SessionEvent.created_at,
)


def is_a_party(viewer_id: UUID) -> Any:
    """The predicate every read here is scoped by.

    Written once and reused rather than retyped into three statements — the
    shape non-negotiable #8 exists for, and the one this repository got wrong
    with ``deleted_at IS NULL`` in five places.
    """
    return or_(Session.mentor_id == viewer_id, Session.mentee_id == viewer_id)


def _after(cursor: tuple[str, UUID], *, ascending: bool) -> Any:
    """The keyset position, as a comparison on ``(starts_at, id)``.

    The cursor's sort key is a timestamp rendered as text, so it has to be
    parsed back. A token that survives base64 decoding but holds something that
    is not a timestamp is still a **client** error, and raising here rather than
    letting ``fromisoformat`` escape turns a 500 into the 422 the envelope
    already documents.
    """
    raw, after_id = cursor
    try:
        after = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc
    position = tuple_(Session.starts_at, Session.id)
    bound = tuple_(literal(after), literal(after_id))
    # The page moves the way the list is ordered: forwards through time for
    # `asc`, backwards for the newest-first default.
    return position > bound if ascending else position < bound


async def list_sessions(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int,
    cursor: tuple[str, UUID] | None = None,
    starts_from: dt.datetime | None = None,
    starts_before: dt.datetime | None = None,
    statuses: Sequence[str] = (),
    ascending: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Every session one user is a party to, newest first unless ``ascending``.

    **Optionally narrowed** to sessions starting in ``[starts_from,
    starts_before)`` and to some statuses — the month view's booked days. The
    filters sit in the same statement the cursor pages, so a page is a page of
    the filtered list. With live statuses the partial per-party indexes serve
    the filter; a range over every status walks ``ix_sessions_starts_at``,
    which is the unfiltered list's own plan.

    **Either party, in one list.** A user may be a mentor and a mentee — dual
    roles are free by design, since authorization is profile existence rather
    than a role column — and "my sessions" means the sessions I am in. The row
    carries both ``mentor_id`` and ``mentee_id``, so a client can tell which
    side the user was on without asking again.

    **Measured before being written this way.** The obvious worry is that
    ``mentor_id = :u OR mentee_id = :u`` uses neither of the per-party indexes.
    It does: at 20,000 sessions PostgreSQL walks ``ix_sessions_starts_at`` for
    the ordered page and combines the two partial indexes with a ``BitmapOr``
    when the status filter applies. No sequential scan, and the either-party
    query measured *faster* than the single-party one because both stop at the
    limit and the union needs no separate sort.

    Newest first: this list includes cancelled and completed sessions, which is
    most of the history, and a paged list of mostly-past rows reads that way.
    ``ascending`` is for an Upcoming view, where the next session must lead page
    one rather than end the last; the cursor flips with it.
    """
    keys = (Session.starts_at, Session.id)
    statement = (
        _with_parties(select(*_SESSION_COLUMNS, *_PARTY_COLUMNS))
        .where(is_a_party(user_id))
        .order_by(*(k.asc() if ascending else k.desc() for k in keys))
    )
    if starts_from is not None:
        statement = statement.where(Session.starts_at >= starts_from)
    if starts_before is not None:
        statement = statement.where(Session.starts_at < starts_before)
    if statuses:
        statement = statement.where(Session.status.in_(list(statuses)))
    if cursor is not None:
        statement = statement.where(_after(cursor, ascending=ascending))

    # One more than asked for: if it comes back there is a next page. Cheaper
    # and more honest than a second COUNT, which can disagree with the page it
    # claims to describe.
    rows = [dict(r) for r in (await session.execute(statement.limit(limit + 1))).mappings()]
    page = rows[:limit]
    await _with_answer_previews(session, page, is_a_party(user_id))
    return page, len(rows) > limit


async def get_session(
    session: AsyncSession, session_id: UUID, viewer_id: UUID
) -> dict[str, Any] | None:
    """One session, scoped to the people in it.

    The viewer is part of the query, not a comparison the caller makes on the
    row it got back. ``None`` means "no such session **or** not yours", and the
    route turns both into the same 404 — distinguishing them tells anyone who
    can enumerate ids which sessions exist.
    """
    result = await session.execute(
        _with_parties(select(*_SESSION_COLUMNS, *_PARTY_COLUMNS)).where(
            Session.id == session_id, is_a_party(viewer_id)
        )
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    found = dict(row)
    await _with_answer_previews(session, [found], is_a_party(viewer_id))
    return found


async def _with_answer_previews(
    session: AsyncSession, rows: list[dict[str, Any]], scope: Any
) -> None:
    """Each row's answer preview, in **one** query for the whole page.

    ``scope`` is the party predicate the rows were read with, repeated in the
    preview's own statement rather than trusted from the ids. A session with no
    answers gets ``None``.
    """
    previews = await answer_previews(session, [row["id"] for row in rows], scope)
    for row in rows:
        row["answers_preview"] = previews.get(row["id"])


async def list_session_events(
    session: AsyncSession, session_id: UUID, viewer_id: UUID
) -> list[dict[str, Any]] | None:
    """One session's history, oldest first, scoped by its session's parties.

    ``session_events`` carries no party of its own, so filtering on
    ``session_id`` alone would return any session's history to anyone holding an
    id — scoped-looking and wide open.

    **The ownership query below is the control, and it is the only one.** An
    earlier version also repeated the viewer predicate inside a join, and a
    mutation batch showed that predicate was unreachable: the check returns
    first, so no test could tell it from its absence. Two mechanisms for one rule
    is what this repository has been burned by, and the docstring then said "the
    join is the authorization" — which had stopped being true one line above it.

    Returning ``None`` rather than ``[]`` is load-bearing and is why the check
    cannot simply be folded into the query: an empty list says "this session
    exists and has no history", which is a different claim and leaks the
    session's existence.
    """
    owned = await session.execute(
        select(Session.id).where(Session.id == session_id, is_a_party(viewer_id))
    )
    if owned.scalar_one_or_none() is None:
        return None

    result = await session.execute(
        select(*_EVENT_COLUMNS)
        .where(SessionEvent.session_id == session_id)
        .order_by(SessionEvent.created_at, SessionEvent.id)
    )
    return [dict(row) for row in result.mappings()]

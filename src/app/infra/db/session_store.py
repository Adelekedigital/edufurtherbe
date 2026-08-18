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
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import Select, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ValidationError
from app.infra.db.models.sessions import Session, SessionEvent
from app.infra.db.models.user import User, UserProfile
from app.infra.db.session_stats import MENTEE, attendance_rate

__all__ = ["get_session", "list_session_events", "list_sessions"]

#: What both parties may read about a session. `created_by` is absent — it is
#: null on every migrated row and is an internal attribution rather than
#: something either party needs.
_SESSION_COLUMNS = (
    Session.id,
    Session.mentor_id,
    Session.mentee_id,
    Session.session_type_id,
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
#: **No `deleted_at` predicate, deliberately.** The authorization here is the
#: session itself: a mentee is a party to it, and their own history should not
#: decay into a UUID because the mentor later left. That is the opposite of the
#: public endpoints, where the mentor's lifecycle *is* the control, and it is a
#: mutation in the batch so a future "add the predicate everywhere" sweep goes red
#: rather than silently rewriting people's history.
_MENTOR = aliased(User, name="mentor_user")
_MENTEE = aliased(User, name="mentee_user")
_MENTOR_PROFILE = aliased(UserProfile, name="mentor_profile")
_MENTEE_PROFILE = aliased(UserProfile, name="mentee_profile")

_PARTY_COLUMNS = (
    _MENTOR.first_name.label("mentor_first_name"),
    _MENTOR.last_name.label("mentor_last_name"),
    _MENTOR_PROFILE.avatar_url.label("mentor_avatar_url"),
    _MENTEE.first_name.label("mentee_first_name"),
    _MENTEE.last_name.label("mentee_last_name"),
    _MENTEE_PROFILE.avatar_url.label("mentee_avatar_url"),
)


def _with_parties(statement: Select[Any]) -> Select[Any]:
    """Attach both people to a session query.

    Written once and applied by both readers, so a client cannot get a named
    mentor from the list and a bare id from the detail. Every join is many-to-one
    on a primary key or a unique `user_id`, so no statement gains a row — which
    matters because the list is keyset-paged and a duplicate would corrupt the
    page rather than merely repeat a name.
    """
    return (
        statement.select_from(Session)
        .join(_MENTOR, _MENTOR.id == Session.mentor_id)
        .join(_MENTEE, _MENTEE.id == Session.mentee_id)
        .outerjoin(_MENTOR_PROFILE, _MENTOR_PROFILE.user_id == Session.mentor_id)
        .outerjoin(_MENTEE_PROFILE, _MENTEE_PROFILE.user_id == Session.mentee_id)
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


def _is_a_party(viewer_id: UUID) -> Any:
    """The predicate every read here is scoped by.

    Written once and reused rather than retyped into three statements — the
    shape non-negotiable #8 exists for, and the one this repository got wrong
    with ``deleted_at IS NULL`` in five places.
    """
    return or_(Session.mentor_id == viewer_id, Session.mentee_id == viewer_id)


def _after(cursor: tuple[str, UUID]) -> Any:
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
    # Descending, so the page moves *backwards* through time.
    return tuple_(Session.starts_at, Session.id) < tuple_(literal(after), literal(after_id))


async def list_sessions(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int,
    cursor: tuple[str, UUID] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Every session one user is a party to, newest first.

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
    most of the history, and a paged list of mostly-past rows reads that way. A
    status filter is additive later and does not change the contract.
    """
    statement = (
        _with_parties(select(*_SESSION_COLUMNS, *_PARTY_COLUMNS))
        .where(_is_a_party(user_id))
        .order_by(Session.starts_at.desc(), Session.id.desc())
    )
    if cursor is not None:
        statement = statement.where(_after(cursor))

    # One more than asked for: if it comes back there is a next page. Cheaper
    # and more honest than a second COUNT, which can disagree with the page it
    # claims to describe.
    rows = [dict(r) for r in (await session.execute(statement.limit(limit + 1))).mappings()]
    return rows[:limit], len(rows) > limit


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
            Session.id == session_id, _is_a_party(viewer_id)
        )
    )
    row = result.mappings().one_or_none()
    return dict(row) if row else None


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
        select(Session.id).where(Session.id == session_id, _is_a_party(viewer_id))
    )
    if owned.scalar_one_or_none() is None:
        return None

    result = await session.execute(
        select(*_EVENT_COLUMNS)
        .where(SessionEvent.session_id == session_id)
        .order_by(SessionEvent.created_at, SessionEvent.id)
    )
    return [dict(row) for row in result.mappings()]

"""The mentors a mentee has met: `GET /me/mentors` (decision 248).

**Composed, never re-derived** (non-negotiable #8). Each fact is read through
the predicate that already answers it elsewhere:

- *met* is `session_stats.received()`, the rule behind `/me`'s
  `mentee_completed_sessions` and the profile's review prompt, grouped by
  mentor;
- *upcoming* and *pending* are `pending_requests.upcoming_session()` and
  `awaiting_mentor()`, the rules behind `/me`'s booking counts;
- *taking bookings* is `public_visibility.taking_bookings()`, the profile's
  flag, and the slug is published only while `mentor_is_live()`, the only time
  `/mentors/{slug}` resolves;
- *exists* is `mentor_exists()`: a mentor whose account or mentor profile is
  soft-deleted is left out (owner, 2026-10-14). Their sessions stay in the
  session history; this list is for booking again.

**One statement**: the grouped sessions, the people, the qualification lateral
and two `EXISTS`, so every figure on a card comes from one snapshot. Scoped to
the caller inside the grouped subquery and inside each `EXISTS`
(non-negotiable #5), so another mentee's sessions are never read at all.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, literal, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User, UserProfile
from app.infra.db.pending_requests import awaiting_mentor, upcoming_session
from app.infra.db.public_visibility import mentor_exists, mentor_is_live, taking_bookings
from app.infra.db.qualifications import top_qualification
from app.infra.db.session_stats import received

__all__ = ["list_met_mentors"]

_QUALIFICATION = top_qualification(User.id, name="mentor_qualification")


def _with_mentor(mentee: UUID, *clauses: Any) -> Any:
    """`EXISTS`: a session between this mentee and the row's mentor matching
    ``clauses``. The mentee is named here, not inherited from the outer query."""
    return (
        select(Session.id)
        .where(Session.mentee_id == mentee, Session.mentor_id == User.id, *clauses)
        .correlate(User)
        .exists()
    )


def _after(met: Any, cursor: tuple[str, UUID]) -> Any:
    """The keyset position on ``(last_session_at, mentor id)``, newest first.

    A token that decodes but holds no timestamp is a client error, a `422`,
    the shape `own_review_reader._after` gives the same case.
    """
    raw, after_id = cursor
    try:
        after = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc
    return tuple_(met.c.last_session_at, User.id) < tuple_(literal(after), literal(after_id))


async def list_met_mentors(
    session: AsyncSession,
    mentee: UUID,
    *,
    now: dt.datetime,
    limit: int,
    cursor: tuple[str, UUID] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of the mentors ``mentee`` has had a completed session with,
    most recent session first, and whether there is another page."""
    met = (
        select(
            Session.mentor_id.label("mentor_id"),
            func.count().label("session_count"),
            func.max(Session.starts_at).label("last_session_at"),
        )
        .where(received(mentee))
        .group_by(Session.mentor_id)
        .subquery("met")
    )
    statement = (
        select(
            User.id,
            case((and_(*mentor_is_live()), User.slug)).label("slug"),
            User.first_name,
            User.last_name,
            UserProfile.avatar_url,
            UserProfile.avatar_focus_x,
            UserProfile.avatar_focus_y,
            _QUALIFICATION.c.degree,
            _QUALIFICATION.c.institution,
            met.c.session_count,
            met.c.last_session_at,
            _with_mentor(mentee, *upcoming_session(now)).label("has_upcoming"),
            _with_mentor(mentee, *awaiting_mentor(now)).label("pending"),
            taking_bookings().label("taking_bookings"),
        )
        .select_from(met)
        .join(User, User.id == met.c.mentor_id)
        # Outer: a mentor with no profile row has not been deleted, and is
        # listed as not taking bookings rather than dropped from the history.
        .outerjoin(MentorProfile, MentorProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        # One row by construction (`limit 1`), so the page is not multiplied.
        .outerjoin(_QUALIFICATION, true())
        .where(*mentor_exists())
        .order_by(met.c.last_session_at.desc(), User.id.desc())
    )
    if cursor is not None:
        statement = statement.where(_after(met, cursor))
    rows = [dict(r) for r in (await session.execute(statement.limit(limit + 1))).mappings()]
    return rows[:limit], len(rows) > limit

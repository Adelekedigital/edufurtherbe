"""The mentors a mentee has met: `GET /me/mentors` (decision 248).

**Composed, never re-derived** (non-negotiable #8). Each fact is read through
the predicate that already answers it elsewhere:

- *met* is `session_stats.received()`, the rule behind `/me`'s
  `mentee_completed_sessions` and the profile's review prompt, grouped by
  mentor;
- *pending* is `pending_requests.awaiting_mentor()`, the rule behind `/me`'s
  booking counts; *has upcoming* is a confirmed session that has not ended
  (`_not_over`), so one in progress counts;
- *taking bookings* is `public_visibility.taking_bookings()`, the profile's
  flag, and the slug is published only while `mentor_is_live()`, the only time
  `/mentors/{slug}` resolves;
- *exists* is `mentor_exists()`: a mentor whose account or mentor profile is
  soft-deleted is left out (owner, 2026-10-14). Their sessions stay in the
  session history; this list is for booking again.

**One statement**: the paged, grouped sessions, then the people, the
qualification lateral and two `EXISTS` for that page only, so every figure on a
card comes from one snapshot. Scoped to
the caller inside the grouped subquery and inside each `EXISTS`
(non-negotiable #5), so another mentee's sessions are never read at all.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SessionStatus
from app.infra.db import keyset
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User, UserProfile
from app.infra.db.pending_requests import awaiting_mentor
from app.infra.db.predicates import session_ends
from app.infra.db.public_visibility import mentor_exists, mentor_is_live, taking_bookings
from app.infra.db.qualifications import top_qualification
from app.infra.db.session_stats import received

__all__ = ["list_met_mentors"]

_QUALIFICATION = top_qualification(User.id, name="mentor_qualification")


def _not_over(now: dt.datetime) -> list[Any]:
    """A confirmed session that has not **ended**: upcoming, or in progress.

    Not `pending_requests.upcoming_session()`, which stops at the start: a
    mentee in the middle of a session is still booked with that mentor, and
    `open` would invite them to book again. The end is `session_window()`'s
    upper bound, the one definition of a session's window.
    """
    return [Session.status == SessionStatus.CONFIRMED, session_ends() > now]


def _with_mentor(mentee: UUID, *clauses: Any) -> Any:
    """`EXISTS`: a session between this mentee and the row's mentor matching
    ``clauses``. The mentee is named here, not inherited from the outer query."""
    return (
        select(Session.id)
        .where(Session.mentee_id == mentee, Session.mentor_id == User.id, *clauses)
        .correlate(User)
        .exists()
    )


async def list_met_mentors(
    session: AsyncSession,
    mentee: UUID,
    *,
    now: dt.datetime,
    limit: int,
    cursor: tuple[str, UUID] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of the mentors ``mentee`` has had a completed session with,
    most recent session first, and whether there is another page.

    **Paged first, then described**: the grouped subquery orders, applies the
    cursor and limits, so the people, the lateral and the `EXISTS` run for one
    page of mentors rather than every mentor ever met. Deleted mentors are
    filtered inside it, before the limit, so a page is never short.

    **The sort key moves.** A mentor whose newer session completes while a
    client is paging jumps to the front, so a traversal already past their old
    position skips them until it starts again. Accepted: the list is short and
    a stable key would need stored state (D56).
    """
    last_session_at = func.max(Session.starts_at)
    met = (
        select(
            Session.mentor_id.label("mentor_id"),
            func.count().label("session_count"),
            last_session_at.label("last_session_at"),
        )
        .select_from(Session)
        .join(User, User.id == Session.mentor_id)
        # Outer: a mentor with no profile row has not been deleted, and is
        # listed as not taking bookings rather than dropped from the history.
        .outerjoin(MentorProfile, MentorProfile.user_id == User.id)
        .where(received(mentee), *mentor_exists())
        .group_by(Session.mentor_id)
        .having(*([keyset.after(last_session_at, Session.mentor_id, cursor)] if cursor else []))
        .order_by(last_session_at.desc(), Session.mentor_id.desc())
        .limit(limit + 1)
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
            UserProfile.cover_color,
            UserProfile.cover_art,
            _QUALIFICATION.c.degree,
            _QUALIFICATION.c.institution,
            met.c.session_count,
            met.c.last_session_at,
            _with_mentor(mentee, *_not_over(now)).label("has_upcoming"),
            _with_mentor(mentee, *awaiting_mentor(now)).label("pending"),
            taking_bookings().label("taking_bookings"),
        )
        .select_from(met)
        .join(User, User.id == met.c.mentor_id)
        .outerjoin(MentorProfile, MentorProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        # One row by construction (`limit 1`), so the page is not multiplied.
        .outerjoin(_QUALIFICATION, true())
        .order_by(met.c.last_session_at.desc(), User.id.desc())
    )
    rows = [dict(r) for r in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit

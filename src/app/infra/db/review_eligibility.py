"""Whether this mentee may review this session — written once, read by three.

**One predicate, three callers**, which is the whole reason this module is not
inlined into the writer. `POST /reviews` refuses on it, the profile picker lists
by it, and the `REVIEW_REQUESTED` producer will suppress on it. Two rules for
one question would eventually disagree, and the disagreement would be a mentee
told to review a session the write then refuses — the exact shape
non-negotiable #8 exists to prevent.

Each part takes its user as a parameter rather than closing over one, following
`session_stats.delivered()`: the picker correlates against a column inside a
larger query and the writer passes a resolved id, and that difference is the
only thing that ever tempts anyone to write it twice.

THE FOUR CLAUSES
================
*completed* · *the caller's own* · *this session not already reviewed* ·
*this offering not reviewed inside* ``REVIEW_INTERVAL``.

**The interval is scoped to the offering, not the mentor.** A mentor who is
excellent at CV review and poor at interview prep is two facts, and a
mentor-wide window collapses them into whichever the mentee happened to book
first. It also removes a collision with the append rule: under a mentor-wide
window two sessions inside a month yield one review, and the second is never
even asked for, because the request producer suppresses on this same predicate.

**A session with no offering escapes the interval clause entirely**, and that is
correct rather than a hole. `sessions.session_type_id` is nullable for the
migrated rows, so there is no offering to compare; the *one review per session*
rule still caps those at one apiece, which is the protection that matters.

**Withdrawn reviews still suppress.** Neither `EXISTS` filters `deleted_at`, and
that is deliberate — a review moderated away still happened, and letting removal
hand the author a fresh slot inverts the incentive. The aggregates take the
opposite view, because a withdrawn review must not move a mentor's average. Both
are the same rule seen from two sides: withdrawal removes a review from what is
*published*, not from what *happened*.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, exists, select

from app.domain.enums import SessionStatus
from app.domain.reviews import REVIEW_INTERVAL
from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session, SessionType

__all__ = ["already_reviewed", "reviewable_sessions", "within_interval"]

#: Aliased so the interval's own join to `sessions` cannot collide with the
#: outer query's, which is what lets the picker compose this against a session
#: it is already selecting from.
_PRIOR = Session.__table__.alias("prior_session")


def already_reviewed(session_id: Any, author: Any) -> Any:
    """Whether this author has already reviewed this session.

    Mirrors `uq_reviews_one_per_session_author`. The constraint is the guarantee
    and this is the *answer* — a caller told "you already did this" rather than
    handed an integrity error, and a picker that never offers the session at all.
    """
    return exists().where(and_(Review.session_id == session_id, Review.reviewed_by == author))


def within_interval(author: Any, session_type_id: Any, now: dt.datetime) -> Any:
    """Whether this author reviewed this offering inside ``REVIEW_INTERVAL``.

    ``now`` is passed rather than read, so the rule is testable at both edges of
    the window without waiting thirty days for one of them.

    **Reads `created_at`, never `updated_at`.** An edit must not extend the
    window; `PATCH` is a ten-minute compose grace period and letting it restart a
    thirty-day suppression would make the two windows one.
    """
    return exists().where(
        and_(
            Review.session_id == _PRIOR.c.id,
            Review.reviewed_by == author,
            # **Both halves, and the second is not redundant.** This function is
            # handed a *column* by the picker and a *value* by the writer, and
            # SQLAlchemy renders `== None` as `IS NULL` while rendering
            # `col == col` as an equality that is never true for two NULLs.
            # Without the guard the two callers disagree on exactly one
            # population — the migrated sessions, which have no offering — so
            # the picker offers a session the writer refuses.
            #
            # Stating it as *the prior review had an offering* makes the rule
            # one sentence on both paths: a review of a session with no
            # offering suppresses nothing, there being no offering to suppress.
            _PRIOR.c.session_type_id.is_not(None),
            _PRIOR.c.session_type_id == session_type_id,
            Review.created_at > now - REVIEW_INTERVAL,
        )
    )


def reviewable_sessions(
    author: UUID, now: dt.datetime, mentor: UUID | None = None, *, limit: int | None = None
) -> Select[Any]:
    """The sessions this mentee may review right now, newest first.

    What the profile's Reviews tab needs in order to ask the right question:
    nothing to ask when the list is empty, no question at all when it holds one,
    and a choice when it holds several.

    ``mentor`` narrows it to one mentor's sessions, which is what the tab wants;
    left out, it answers "what can I review at all", which is what a future
    dashboard prompt would want. One query rather than two, because the clauses
    that matter are identical and a second copy would drift.
    """
    return (
        select(
            Session.id.label("session_id"),
            Session.mentor_id,
            Session.starts_at,
            Session.session_type_id,
            SessionType.name.label("session_type_name"),
        )
        .join(SessionType, SessionType.id == Session.session_type_id, isouter=True)
        .where(
            Session.mentee_id == author,
            Session.status == SessionStatus.COMPLETED,
            ~already_reviewed(Session.id, author),
            ~within_interval(author, Session.session_type_id, now),
            *([Session.mentor_id == mentor] if mentor is not None else []),
        )
        # **`id` breaks the tie, and it is not decoration.** Two sessions can
        # start at the same instant for one mentee — different mentors, or a
        # migrated pair — and an unstable order under a `Page` contract shows
        # one row twice and hides another. The same reason `_ranked` orders by
        # rank *then* id.
        .order_by(Session.starts_at.desc(), Session.id.desc())
        # Bounded, because the envelope promises a page. A mentee carrying a
        # long migrated history would otherwise get every row of it in one
        # response that says `next_cursor: null`.
        .limit(limit)
    )

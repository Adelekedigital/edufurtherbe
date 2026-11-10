"""The admin Analytics Sessions tab, and the session figures Overview shares
(PRD 31, decision 324).

**A request is a session no move made** (`rescheduled_from_session_id IS
NULL`), placed by when it was made. **What a request became** is the status of
the newest session in its move chain (`request_outcomes`), through
`credit_store.paying_sessions`, the one statement of a move chain: a request
moved twice and then held counts as completed, once.

**Completed** is `session_stats.completed()`, by `starts_at`. A decision is a
`session_events` row leaving `pending_mentor_approval` for `confirmed`,
`declined` or `expired`, by the event's own time. Reviews are `published`
reviews of a mentor (`review_stats`), by when they were written; their value
is `review_value()`, the badge's. Everything leaves out whom
`analytics_scope` leaves out.

Every figure is a function of a window returning a scalar subquery, so one
statement reads it for any number of windows (`analytics_scope.per_window`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, and_, cast, func, select

from app.domain.enums import SessionStatus
from app.domain.mentor_stats import Window
from app.infra.db.analytics_scope import counted_session, counted_user
from app.infra.db.credit_store import paying_sessions
from app.infra.db.models.missed_reasons import SessionMissedReason
from app.infra.db.models.review_reports import ReviewReport
from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session, SessionEvent
from app.infra.db.predicates import during
from app.infra.db.review_stats import published, review_value
from app.infra.db.session_stats import completed

#: The statuses a request can be decided into, from waiting on the mentor.
ACCEPTED = SessionStatus.CONFIRMED
DECIDED = (SessionStatus.CONFIRMED, SessionStatus.DECLINED, SessionStatus.EXPIRED)
#: How requests end, as the tab draws them (still pending or upcoming: not drawn).
OUTCOMES = (
    SessionStatus.COMPLETED,
    SessionStatus.CANCELLED,
    SessionStatus.EXPIRED,
    SessionStatus.NO_SHOW,
    SessionStatus.DECLINED,
)


def _count(where: Any, *, of: Any = None) -> Any:
    counted = func.count() if of is None else func.count(func.distinct(of))
    return select(counted).select_from(Session).where(where).scalar_subquery()


def is_request() -> Any:
    """A session that was asked for, not made by a move, between counted people."""
    return and_(Session.rescheduled_from_session_id.is_(None), counted_session())


def request_outcomes(window: Window) -> Any:
    """Each request made in ``window``: its id (``root_id``) and what it became
    (``status``), the status of the newest session in its move chain. Each call
    names its chain apart, so one statement can hold several windows."""
    chain = paying_sessions(
        None,
        roots=and_(counted_session(), during(Session.created_at, window)),
        name=f"request_chain_{window.since:%Y%m%d%H%M%S%f}_{window.until:%Y%m%d%H%M%S%f}",
    )
    return (
        select(chain.c.payer_id.label("root_id"), Session.status.label("status"))
        .join(Session, Session.id == chain.c.session_id)
        .distinct(chain.c.payer_id)
        .order_by(chain.c.payer_id, Session.created_at.desc(), Session.id.desc())
        .subquery("request_outcomes")
    )


def requests(window: Window, *, became: SessionStatus | None = None) -> Any:
    """Requests made in ``window``; with ``became``, only those that ended so."""
    where = [is_request(), during(Session.created_at, window)]
    if became is None:
        return _count(and_(*where))
    outcome = request_outcomes(window)
    return (
        select(func.count())
        .select_from(Session)
        .join(outcome, outcome.c.root_id == Session.id)
        .where(*where, outcome.c.status == became)
        .scalar_subquery()
    )


def is_started_in(window: Window, status: Any) -> Any:
    """A counted session starting in ``window`` with ``status`` (a predicate)."""
    return and_(status, counted_session(), during(Session.starts_at, window))


def started(window: Window, status: Any) -> Any:
    """Counted sessions starting in ``window`` with ``status`` (a predicate)."""
    return _count(is_started_in(window, status))


def is_completed_in(window: Window) -> Any:
    """A counted completed session starting in ``window``."""
    return is_started_in(window, completed())


def completed_in(window: Window, *, distinct: Any = None) -> Any:
    """Completed sessions starting in ``window``, or the distinct ``distinct``
    among them (a mentee or a mentor column)."""
    return _count(is_completed_in(window), of=distinct)


def no_shows_in(window: Window) -> Any:
    return started(window, Session.status == SessionStatus.NO_SHOW)


def _decisions(window: Window, into: tuple[SessionStatus, ...]) -> Any:
    return and_(
        SessionEvent.session_id == Session.id,
        SessionEvent.from_status == SessionStatus.PENDING_MENTOR_APPROVAL,
        SessionEvent.to_status.in_(into),
        during(SessionEvent.created_at, window),
        counted_session(),
    )


def decided(window: Window, into: tuple[SessionStatus, ...] = DECIDED) -> Any:
    """Requests decided in ``window`` into one of ``into``."""
    return (
        select(func.count())
        .select_from(SessionEvent)
        .join(Session, Session.id == SessionEvent.session_id)
        .where(_decisions(window, into))
        .scalar_subquery()
    )


def median_reply_hours(window: Window) -> Any:
    """Median hours from a request to the mentor's accept or decline, for
    replies in ``window``; ``NULL`` with none."""
    hours = func.extract("epoch", SessionEvent.created_at - Session.created_at) / 3600
    return (
        select(func.percentile_cont(0.5).within_group(cast(hours, Float)))
        .select_from(SessionEvent)
        .join(Session, Session.id == SessionEvent.session_id)
        .where(_decisions(window, (SessionStatus.CONFIRMED, SessionStatus.DECLINED)))
        .scalar_subquery()
    )


def repeat_mentees(window: Window, *, at_least: int) -> Any:
    """Mentees with ``at_least`` completed sessions starting in ``window``."""
    per_mentee = (
        select(Session.mentee_id)
        .where(is_completed_in(window))
        .group_by(Session.mentee_id)
        .having(func.count() >= at_least)
        .subquery()
    )
    return select(func.count()).select_from(per_mentee).scalar_subquery()


def _written(window: Window) -> Any:
    """Reviews written in ``window`` that count publicly: `published()`, the
    badge's rule, for whichever mentor each is about."""
    return and_(
        published(Review.reviewed_for),
        during(Review.created_at, window),
        counted_user(Review.reviewed_by),
        counted_user(Review.reviewed_for),
    )


def reviews_written(window: Window) -> Any:
    return select(func.count()).select_from(Review).where(_written(window)).scalar_subquery()


def average_rating(window: Window) -> Any:
    """Mean `review_value()` of reviews written in ``window``, or ``NULL``."""
    value = cast(review_value(), Float)
    return select(func.avg(value)).select_from(Review).where(_written(window)).scalar_subquery()


def reports(window: Window) -> Any:
    """Review reports made in ``window`` by counted people."""
    return (
        select(func.count())
        .select_from(ReviewReport)
        .where(during(ReviewReport.created_at, window), counted_user(ReviewReport.reported_by))
        .scalar_subquery()
    )


def missed_reasons(window: Window) -> Any:
    """Each reason given for a counted session starting in ``window``, with how
    many gave it: most first, then by name."""
    reason = SessionMissedReason.reason
    return (
        select(reason, func.count())
        .join(Session, Session.id == SessionMissedReason.session_id)
        .where(counted_session(), during(Session.starts_at, window))
        .group_by(reason)
        .order_by(func.count().desc(), reason)
    )


def outcome_counts(window: Window) -> Any:
    """Requests made in ``window`` by what they became."""
    outcome = request_outcomes(window)
    return (
        select(outcome.c.status, func.count())
        .select_from(Session)
        .join(outcome, outcome.c.root_id == Session.id)
        .where(is_request(), during(Session.created_at, window))
        .group_by(outcome.c.status)
    )

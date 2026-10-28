"""The admin Mentors page: four tabs, their counts, and one mentor's drawer.

**Reads other people's rows by design**, like `admin_store`: the control is the
caller's grant, checked in `api.deps.require_admin` before anything here runs.

Every figure is a predicate that already answers it elsewhere (non-negotiable
#8): the tabs are the application predicates, the session counts are
`session_stats`' and `pending_requests`', the dates are the status log's own
readers, and the show-up band is `domain.admin_mentors.show_up_tier`.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.admin_mentors import (
    AdminMentorTab,
    PausedBy,
    ShowUpTier,
    show_up_tier,
)
from app.domain.enums import ListingStatus, MentorStatusType
from app.domain.refunds import BOOKED_STATUSES
from app.infra.db.availability_store import list_rules
from app.infra.db.catalogue_store import LIKE_ESCAPE, escape_like
from app.infra.db.mentor_listing import newest_unlisting, paused_by_mentor
from app.infra.db.models.mentoring import MentorProfile, MentorStatusEvent
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User, UserProfile
from app.infra.db.offerings import offerings_for
from app.infra.db.pending_requests import upcoming_session
from app.infra.db.predicates import (
    LIVE,
    application_approved,
    application_declined,
    application_in_review,
    live,
    mentor_paused,
    name_like,
)
from app.infra.db.profile_store import latest_decision, list_education
from app.infra.db.qualifications import top_qualification
from app.infra.db.session_stats import MENTOR, attendance_rate, held, missed, missed_by

__all__ = ["mentor_detail", "mentor_rows", "mentors_summary", "tab_statement"]

#: Who each tab holds, on a live profile. No mentor matches two: in review is
#: pending, the next two split approved by listing, the last is declined.
TABS: dict[AdminMentorTab, list[Any]] = {
    AdminMentorTab.APPLICATIONS: application_in_review(),
    AdminMentorTab.APPROVED: [
        *application_approved(),
        MentorProfile.listing_status == ListingStatus.LISTED,
    ],
    AdminMentorTab.PAUSED: mentor_paused(),
    AdminMentorTab.NOT_APPROVED: application_declined(),
}


def _tab_column() -> Any:
    """The tab a profile is in, as a column: the same predicates, so a drawer
    can never name a tab the list would not put the mentor in."""
    return case(
        *[(and_(*clauses), tab.value) for tab, clauses in TABS.items()],
        else_=None,
    )


def _completed() -> Any:
    """How many sessions this mentor delivered, all time (the profile's figure)."""
    return (
        select(func.count())
        .select_from(Session)
        .where(held(MentorProfile.user_id, None))
        .correlate(MentorProfile)
        .scalar_subquery()
    )


def _upcoming(now: dt.datetime) -> Any:
    """Confirmed sessions of this mentor's that have not started (`/me`'s rule)."""
    return (
        select(func.count())
        .select_from(Session)
        .where(Session.mentor_id == MentorProfile.user_id, *upcoming_session(now))
        .correlate(MentorProfile)
        .scalar_subquery()
    )


def _missed_by_mentor() -> Any:
    """Settled sessions this mentor missed, all time (the dashboard's rule)."""
    return (
        select(func.count())
        .select_from(Session)
        .where(missed(MentorProfile.user_id, None))
        .correlate(MentorProfile)
        .scalar_subquery()
    )


def _is_paused() -> Any:
    return and_(*TABS[AdminMentorTab.PAUSED])


def _row_statement(now: dt.datetime) -> Any:
    """One row per mentor, with every column a tab or the drawer shows."""
    school = top_qualification(MentorProfile.user_id, name="admin_first_school")
    paused = _is_paused()
    self_paused = paused_by_mentor()
    return (
        select(
            User.id.label("user_id"),
            User.slug.label("handle"),
            User.first_name,
            User.last_name,
            User.email,
            User.timezone,
            UserProfile.avatar_url,
            UserProfile.cover_color,
            UserProfile.about_me,
            UserProfile.social_linkedin,
            MentorProfile.headline,
            MentorProfile.submitted_at,
            _tab_column().label("tab"),
            latest_decision(MentorStatusEvent.created_at, MentorStatusType.APPROVED).label(
                "approved_at"
            ),
            latest_decision(MentorStatusEvent.created_at, MentorStatusType.DECLINED).label(
                "decided_at"
            ),
            latest_decision(MentorStatusEvent.reason, MentorStatusType.DECLINED).label(
                "decline_reason"
            ),
            case((paused, newest_unlisting(MentorStatusEvent.created_at)), else_=None).label(
                "paused_at"
            ),
            case(
                (paused & self_paused, PausedBy.MENTOR.value),
                (paused, PausedBy.ADMIN.value),
                else_=None,
            ).label("paused_by"),
            # An admin's reason is for admins; a mentor's own pause has none to show.
            case(
                (paused & ~self_paused, newest_unlisting(MentorStatusEvent.reason)), else_=None
            ).label("paused_reason"),
            school.c.institution.label("school_name"),
            school.c.institution_country.label("school_country"),
            _completed().label("completed_sessions"),
            _upcoming(now).label("upcoming_sessions"),
            _missed_by_mentor().label("missed_by_mentor"),
            attendance_rate(MentorProfile.user_id, MENTOR).label("show_up_rate"),
        )
        .select_from(MentorProfile)
        .join(User, User.id == MentorProfile.user_id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .outerjoin(school, true())
        .where(LIVE)
    )


def _matches(q: str) -> Any:
    """``q`` inside the mentor's name or email, ignoring case."""
    pattern = "%" + escape_like(q) + "%"
    return or_(name_like(User, q), User.email.ilike(pattern, escape=LIKE_ESCAPE))


def tab_statement(tab: AdminMentorTab, *, now: dt.datetime, q: str | None = None) -> Any:
    """The statement behind `mentor_rows`, for the plan test to `EXPLAIN`."""
    statement = _row_statement(now).where(*TABS[tab])
    if q:
        statement = statement.where(_matches(q))
    order = (
        MentorProfile.submitted_at.asc()
        if tab == AdminMentorTab.APPLICATIONS
        else MentorProfile.created_at.desc()
    )
    return statement.order_by(order, MentorProfile.user_id)


async def mentor_rows(
    session: AsyncSession, tab: AdminMentorTab, *, now: dt.datetime, q: str | None = None
) -> list[dict[str, Any]]:
    """Every mentor in ``tab``, narrowed by ``q`` when given.

    Whole, not paged: the admin filters and sorts in the browser (Admin PRD).
    Ordered as the tab's default sort, so an unsorted client still reads it
    right: the longest wait first on Applications, newest profile first
    elsewhere.
    """
    statement = tab_statement(tab, now=now, q=q)
    return [dict(row) for row in (await session.execute(statement)).mappings()]


def _average(rates: list[int]) -> int | None:
    """The mean of whole-percent rates, rounded half away from zero (#171)."""
    if not rates:
        return None
    mean = Decimal(sum(rates)) / Decimal(len(rates))
    return int(mean.quantize(Decimal(1), rounding=ROUND_HALF_UP))


async def mentors_summary(session: AsyncSession) -> dict[str, Any]:
    """The tab counts, the longest wait and the approved mentors' show-up."""
    counts = (
        (
            await session.execute(
                select(
                    *[
                        func.count().filter(and_(*clauses)).label(tab.value)
                        for tab, clauses in TABS.items()
                    ],
                    func.min(MentorProfile.submitted_at)
                    .filter(and_(*TABS[AdminMentorTab.APPLICATIONS]))
                    .label("oldest_submitted_at"),
                )
                .select_from(MentorProfile)
                .join(User, User.id == MentorProfile.user_id)
                .where(LIVE)
            )
        )
        .mappings()
        .one()
    )
    rates = [
        int(rate)
        for rate in (
            await session.scalars(
                select(attendance_rate(MentorProfile.user_id, MENTOR))
                .select_from(MentorProfile)
                .join(User, User.id == MentorProfile.user_id)
                .where(LIVE, *TABS[AdminMentorTab.APPROVED])
            )
        )
        if rate is not None
    ]
    return {
        "counts": {tab.value: counts[tab.value] for tab in TABS},
        "oldest_submitted_at": counts["oldest_submitted_at"],
        "approved_average_show_up": _average(rates),
        "at_risk": sum(1 for rate in rates if show_up_tier(rate) == ShowUpTier.AT_RISK),
    }


async def _recent_bookings(session: AsyncSession, mentor: UUID) -> list[dict[str, Any]]:
    """This mentor's five latest booked sessions, newest start first.

    The mentee is joined through `live()`, so a mentee who deleted their
    account keeps the row and loses their name (decision 212).
    """
    mentee = aliased(User)
    statement = (
        select(
            Session.id,
            Session.starts_at,
            Session.status,
            mentee.first_name.label("mentee_first_name"),
            mentee.last_name.label("mentee_last_name"),
            missed_by(Session.mentor_id).label("mentor_missed"),
            missed_by(Session.mentee_id).label("mentee_missed"),
        )
        .outerjoin(mentee, (mentee.id == Session.mentee_id) & live(mentee))
        .where(Session.mentor_id == mentor, Session.status.in_(BOOKED_STATUSES))
        .order_by(Session.starts_at.desc(), Session.id.desc())
        .limit(5)
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]


async def mentor_detail(
    session: AsyncSession, mentor: UUID, *, now: dt.datetime
) -> dict[str, Any] | None:
    """One mentor's drawer, or ``None`` when they are in no tab.

    A profile saved partway through onboarding, a deleted one, and a user who
    is not a mentor are all ``None``: the drawer only opens from a tab.
    """
    row = (
        (
            await session.execute(
                _row_statement(now).where(
                    MentorProfile.user_id == mentor, _tab_column().is_not(None)
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    grouped = await offerings_for(session, [mentor])
    return dict(row) | {
        "education": await list_education(session, mentor),
        "help_areas": grouped.get(mentor, []),
        "weekly_hours": await list_rules(session, mentor),
        "recent_bookings": await _recent_bookings(session, mentor),
    }

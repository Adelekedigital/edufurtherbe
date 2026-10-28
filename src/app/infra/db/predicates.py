"""Row-visibility predicates, defined once and imported by every store.

``LIVE`` began in ``provisioning_store.py`` as a single expression object, put
there after the same rule had been hand-typed into five statements and missed on
the fifth — the ``UPDATE``, so a user soft-deleted mid-run would have been handed
a live Supabase account.

This module exists because a second store now needs it, which is exactly the
extract-on-the-second-occurrence case non-negotiable #8 names. Every statement
that reads or writes an existing ``users`` row composes ``LIVE``; the parity test
in ``tests/unit/test_predicates.py`` walks each store's declared statements and
fails any that omits it.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import and_, exists, func, true

from app.domain.attendance import PRESENCE_REPORTING
from app.domain.enums import ApprovalStatus, ListingStatus
from app.infra.db.catalogue_store import LIKE_ESCAPE, escape_like
from app.infra.db.models.admin import AdminUser
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User


def session_ends() -> Any:
    """When a session is over, in SQL: the upper bound of ``session_window()``,
    the function the double-booking constraint uses, so "when a session ends"
    has one definition. Read by the calendar retries, the offering retirement,
    met-mentors and measured minutes."""
    return func.upper(func.session_window(Session.starts_at, Session.duration_minutes))


def presence_decides_sql() -> Any:
    """:func:`app.domain.attendance.presence_decides` in SQL, held to it by a
    test: a venue that reports presence, and a room that exists. Here rather
    than in the attendance writer so the session read can use it too (#394)."""
    return and_(
        Session.meeting_provider.in_([str(p) for p in PRESENCE_REPORTING]),
        Session.external_room_id.is_not(None),
    )


def live(user: Any) -> Any:
    """``LIVE`` for an aliased ``users`` — a statement joining two people.

    One definition: ``LIVE`` below is this applied to ``User`` itself, so the
    rule cannot mean one thing for the mentor alias and another everywhere else.
    """
    return user.deleted_at.is_(None)


#: "This user still exists." An expression object rather than a string, so a
#: statement that omits it is missing a *name* — something a reader and a test
#: can both see — rather than missing a substring nobody notices.
LIVE = live(User)


def live_grant() -> Any:
    """An admin grant that still stands: not revoked. The one statement of it,
    read by the deciding-admins lookup and the profile's admin widening; the
    `/me` lookup in `api/deps/core.CURRENT_USER` is raw SQL and spells the same
    `revoked_at IS NULL` (pinned by a test in `test_admin_mentors.py`)."""
    return AdminUser.revoked_at.is_(None)


def has_mentor_profile(user_id: Any) -> Any:
    """Whether `user_id` has a mentor profile at all, soft-deleted or not: the
    fact `finishes_as_mentee` reads, so onboarding and the bio draft agree."""
    return exists().where(MentorProfile.user_id == user_id)


def application_in_review() -> list[Any]:
    """A mentor application submitted and awaiting a decision, on a live profile.

    The one definition: the admin queue lists these, and they keep a help area
    as an approved mentor does (decisions 271, 296).
    """
    return [
        MentorProfile.approval_status == ApprovalStatus.PENDING,
        MentorProfile.submitted_at.is_not(None),
        MentorProfile.deleted_at.is_(None),
    ]


def mentor_approved() -> Any:
    """The mentor was approved, listed or not: the one statement of it, as an
    expression so a `SELECT` can read it as a column too."""
    return MentorProfile.approval_status == ApprovalStatus.APPROVED


def application_approved() -> list[Any]:
    """An approved mentor, on a live profile, listed or not."""
    return [mentor_approved(), MentorProfile.deleted_at.is_(None)]


def mentor_paused() -> list[Any]:
    """**The one statement of a paused mentor**: approved, on a live profile,
    and unlisted, whether they paused themselves or an admin did. The admin
    Paused tab lists these (decision 297), and a chat with one is read only
    (decision 301)."""
    return [*application_approved(), MentorProfile.listing_status == ListingStatus.UNLISTED]


def name_like(user: Any, term: str) -> Any:
    """``term`` anywhere in ``user``'s full name, ignoring case, with `LIKE`
    wildcards escaped: the one name match the admin searches and the people
    picker share. The full name covers the first and last names on their own."""
    pattern = "%" + escape_like(term) + "%"
    return func.concat_ws(" ", user.first_name, user.last_name).ilike(pattern, escape=LIKE_ESCAPE)


def application_declined() -> list[Any]:
    """A mentor application that was declined, on a live profile: its booking
    setup is locked (owner, 2026-10-20)."""
    return [
        MentorProfile.approval_status == ApprovalStatus.DECLINED,
        MentorProfile.deleted_at.is_(None),
    ]


def blank(column: Any) -> Any:
    """Null, empty or only whitespace: the one definition of an unwritten text
    field (the bio draft and the mentor application both read it)."""
    return func.coalesce(func.btrim(column), "") == ""


class Bounds(Protocol):
    """A window's two edges: instants (`Window`) or columns of a statement,
    such as the monthly series' `VALUES` list of months (decision 247)."""

    @property
    def since(self) -> Any: ...

    @property
    def until(self) -> Any: ...


def during(column: Any, window: Bounds | None) -> Any:
    """``column`` inside a half-open ``[since, until)`` window, or no condition
    at all for ``None`` (all time).

    One statement of the boundary, so the session figures and the review
    figures of one dashboard window, and every month of the series, cannot
    disagree about which edge is in.
    """
    if window is None:
        return true()
    return and_(column >= window.since, column < window.until)

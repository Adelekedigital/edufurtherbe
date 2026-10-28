"""The admin Mentors page's pure rules (decision 297): the show-up bands, the
wait, what a booking came to, and which grant a person is reported as."""

from __future__ import annotations

import datetime as dt

import pytest

from app.api.deps.core import CURRENT_USER
from app.domain.admin_mentors import (
    BookingOutcome,
    ShowUpTier,
    booking_outcome,
    show_up_tier,
    strongest_role,
    waiting_days,
)
from app.domain.enums import AdminRole, SessionStatus
from app.domain.refunds import BOOKED_STATUSES, NEVER_AGREED
from app.infra.db.predicates import live_grant

NOW = dt.datetime(2026, 10, 26, 12, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("rate", "tier"),
    [
        (None, None),
        (100, ShowUpTier.ON_TRACK),
        (80, ShowUpTier.ON_TRACK),
        (79, ShowUpTier.NEEDS_ATTENTION),
        (50, ShowUpTier.NEEDS_ATTENTION),
        (49, ShowUpTier.AT_RISK),
        (0, ShowUpTier.AT_RISK),
    ],
)
def test_a_rate_bands_by_the_mentor_home_rule(rate: int | None, tier: ShowUpTier | None) -> None:
    assert show_up_tier(rate) == tier


@pytest.mark.parametrize(
    ("ago", "days"),
    [
        (dt.timedelta(0), 0),
        (dt.timedelta(hours=23, minutes=59), 0),
        (dt.timedelta(days=1), 1),
        (dt.timedelta(days=6, hours=23), 6),
        # A submission stamped a moment ahead of the clock is not negative.
        (-dt.timedelta(seconds=5), 0),
    ],
)
def test_the_wait_is_whole_days_rounded_down(ago: dt.timedelta, days: int) -> None:
    assert waiting_days(NOW - ago, NOW) == days


def test_nothing_submitted_has_no_wait() -> None:
    assert waiting_days(None, NOW) is None


@pytest.mark.parametrize(
    ("status", "mentor_missed", "mentee_missed", "outcome"),
    [
        (SessionStatus.CONFIRMED, False, False, BookingOutcome.UPCOMING),
        (SessionStatus.COMPLETED, False, False, BookingOutcome.COMPLETED),
        (SessionStatus.NO_SHOW, True, False, BookingOutcome.MISSED_BY_MENTOR),
        (SessionStatus.NO_SHOW, False, True, BookingOutcome.MISSED_BY_MENTEE),
        # Both missed: the drawer is about the mentor.
        (SessionStatus.NO_SHOW, True, True, BookingOutcome.MISSED_BY_MENTOR),
        # Neither row recorded: not the mentor's miss, as their stats agree.
        (SessionStatus.NO_SHOW, False, False, BookingOutcome.MISSED_BY_MENTEE),
        (SessionStatus.CANCELLED, False, False, BookingOutcome.CANCELED),
    ],
)
def test_a_booking_is_tagged_by_its_status_and_who_missed_it(
    status: SessionStatus, mentor_missed: bool, mentee_missed: bool, outcome: BookingOutcome
) -> None:
    tagged = booking_outcome(
        status,
        starts_at=NOW + dt.timedelta(hours=1),
        now=NOW,
        mentor_missed=mentor_missed,
        mentee_missed=mentee_missed,
    )

    assert tagged == outcome


@pytest.mark.parametrize(
    ("starts_in", "outcome"),
    [
        (dt.timedelta(seconds=1), BookingOutcome.UPCOMING),
        # Started, so no longer what `stats.upcoming` counts.
        (dt.timedelta(0), BookingOutcome.AWAITING_OUTCOME),
        (-dt.timedelta(minutes=10), BookingOutcome.AWAITING_OUTCOME),
    ],
)
def test_a_confirmed_session_is_upcoming_only_until_it_starts(
    starts_in: dt.timedelta, outcome: BookingOutcome
) -> None:
    tagged = booking_outcome(
        SessionStatus.CONFIRMED,
        starts_at=NOW + starts_in,
        now=NOW,
        mentor_missed=False,
        mentee_missed=False,
    )

    assert tagged == outcome


def test_every_status_is_booked_or_not_by_one_rule() -> None:
    """Booked is the complement of a waiting request and the refunds'
    never-agreed set: each status on exactly one side."""
    not_booked = NEVER_AGREED | {SessionStatus.PENDING_MENTOR_APPROVAL}

    assert set(BOOKED_STATUSES) | not_booked == set(SessionStatus)
    assert not set(BOOKED_STATUSES) & not_booked
    assert set(BOOKED_STATUSES) == {
        SessionStatus.CONFIRMED,
        SessionStatus.COMPLETED,
        SessionStatus.NO_SHOW,
        SessionStatus.CANCELLED,
    }


def test_live_grant_matches_current_user() -> None:
    """`/me`'s raw SQL and the SQLAlchemy predicate state one rule."""
    assert str(live_grant().compile()) == "admin_users.revoked_at IS NULL"
    assert "g.revoked_at IS NULL" in str(CURRENT_USER)


@pytest.mark.parametrize(
    ("held", "role"),
    [
        ((), None),
        (("limited_access",), AdminRole.LIMITED_ACCESS),
        (("limited_access", "mentor_approval"), AdminRole.MENTOR_APPROVAL),
        (("mentor_approval", "super_admin", "limited_access"), AdminRole.SUPER_ADMIN),
    ],
)
def test_the_strongest_grant_is_reported(held: tuple[str, ...], role: AdminRole | None) -> None:
    assert strongest_role(held) == role


def test_every_grant_has_a_strength() -> None:
    """A role added to `AdminRole` without a place in the order would read as
    no grant at all on `/me`."""
    for role in AdminRole:
        assert strongest_role([role.value]) == role

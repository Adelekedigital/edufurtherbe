"""The refund rule, at its boundaries (decision 229)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.config import Settings
from app.domain.attendance import absent_party
from app.domain.enums import CreditReason, SessionRole, SessionStatus
from app.domain.refunds import (
    RefundPolicy,
    never_agreed_refund,
    no_show_refund,
    refund_policy,
    transition_refund,
)

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)
#: The deployment default: `MENTEE_CANCEL_REFUND_HOURS` unset.
POLICY = refund_policy(Settings(_env_file=None))  # type: ignore[call-arg]
NOTICE = POLICY.mentee_cancel_notice


def cancelled(actor: SessionRole | None, notice: dt.timedelta) -> CreditReason | None:
    return transition_refund(
        SessionStatus.CANCELLED, actor=actor, starts_at=NOW + notice, now=NOW, policy=POLICY
    )


def test_the_mentee_notice_is_twelve_hours() -> None:
    assert dt.timedelta(hours=12) == NOTICE


def test_a_mentee_cancelling_at_exactly_the_notice_is_refunded() -> None:
    assert cancelled(SessionRole.MENTEE, NOTICE) is CreditReason.SESSION_CANCELLED_REFUND


def test_a_mentee_cancelling_one_second_inside_the_notice_is_not() -> None:
    assert cancelled(SessionRole.MENTEE, NOTICE - dt.timedelta(seconds=1)) is None


def test_a_mentee_cancelling_well_ahead_is_refunded() -> None:
    assert (
        cancelled(SessionRole.MENTEE, dt.timedelta(days=3)) is CreditReason.SESSION_CANCELLED_REFUND
    )


@pytest.mark.parametrize("notice", [dt.timedelta(minutes=11), dt.timedelta(days=3)])
def test_a_mentor_cancelling_always_refunds(notice: dt.timedelta) -> None:
    assert cancelled(SessionRole.MENTOR, notice) is CreditReason.SESSION_CANCELLED_REFUND


def test_nobody_else_cancelling_refunds() -> None:
    assert cancelled(None, dt.timedelta(days=3)) is None


@pytest.mark.parametrize(
    "to", [SessionStatus.DECLINED, SessionStatus.WITHDRAWN, SessionStatus.EXPIRED]
)
def test_a_request_that_never_became_a_session_refunds(to: SessionStatus) -> None:
    assert (
        transition_refund(to, actor=None, starts_at=NOW, now=NOW, policy=POLICY)
        is CreditReason.REQUEST_UNFULFILLED
    )


def test_accepting_refunds_nothing() -> None:
    assert (
        transition_refund(
            SessionStatus.CONFIRMED, actor=SessionRole.MENTOR, starts_at=NOW, now=NOW, policy=POLICY
        )
        is None
    )


def test_the_window_is_a_parameter() -> None:
    six = RefundPolicy(mentee_cancel_notice=dt.timedelta(hours=6))
    assert (
        transition_refund(
            SessionStatus.CANCELLED,
            actor=SessionRole.MENTEE,
            starts_at=NOW + dt.timedelta(hours=7),
            now=NOW,
            policy=six,
        )
        is CreditReason.SESSION_CANCELLED_REFUND
    )


@pytest.mark.parametrize(
    ("mentor_came", "mentee_came", "expected"),
    [
        (False, True, CreditReason.SESSION_NO_SHOW_REFUND),
        (True, False, None),
        (False, False, None),
        (True, True, None),
    ],
)
def test_only_a_mentor_no_show_refunds(
    mentor_came: bool, mentee_came: bool, expected: CreditReason | None
) -> None:
    assert no_show_refund(mentor_came=mentor_came, mentee_came=mentee_came) is expected


@pytest.mark.parametrize(
    ("mentor_attended", "mentee_attended", "expected"),
    [
        (False, True, SessionRole.MENTOR),
        (True, False, SessionRole.MENTEE),
        (False, False, None),
        (True, True, None),
    ],
)
def test_the_absent_party_is_named_only_when_one_missed(
    mentor_attended: bool, mentee_attended: bool, expected: SessionRole | None
) -> None:
    assert (
        absent_party(mentor_attended=mentor_attended, mentee_attended=mentee_attended) is expected
    )


@pytest.mark.parametrize(
    ("to", "expected"),
    [
        (SessionStatus.EXPIRED, CreditReason.REQUEST_UNFULFILLED),
        (SessionStatus.DECLINED, CreditReason.REQUEST_UNFULFILLED),
        (SessionStatus.WITHDRAWN, CreditReason.REQUEST_UNFULFILLED),
        (SessionStatus.CANCELLED, None),
        (SessionStatus.CONFIRMED, None),
    ],
)
def test_only_a_request_that_never_became_a_session_refunds_without_a_clock(
    to: SessionStatus, expected: CreditReason | None
) -> None:
    assert never_agreed_refund(to) is expected

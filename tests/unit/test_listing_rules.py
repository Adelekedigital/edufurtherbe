"""The return-date rule for a self-pause, without a database."""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.listing import (
    RETURN_REMINDER_OFFSETS,
    UNDATED_REMINDER_DAYS,
    cadence,
    first_reminder_stage,
    latest_due_stage,
    return_on_problem,
    stage_after,
    stage_before,
    stage_missed,
)

TODAY = dt.date(2026, 10, 1)


@pytest.mark.parametrize("days", [0, -1, -30])
def test_today_or_earlier_is_refused(days: int) -> None:
    assert return_on_problem(TODAY + dt.timedelta(days=days), TODAY) is not None


def test_tomorrow_is_the_earliest_allowed() -> None:
    assert return_on_problem(TODAY + dt.timedelta(days=1), TODAY) is None


def test_not_sure_yet_is_always_allowed() -> None:
    assert return_on_problem(None, TODAY) is None


def test_the_cadences_are_a_week_three_days_the_day_and_thirty_fifty_nine() -> None:
    assert RETURN_REMINDER_OFFSETS == (7, 3, 0)
    assert UNDATED_REMINDER_DAYS == (30, 59)


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute))


@pytest.mark.parametrize(
    ("days_away", "first"),
    [(10, 7), (8, 7), (7, 3), (5, 3), (3, 0), (2, 0), (1, 0)],
)
def test_the_first_stage_is_the_first_still_ahead(days_away: int, first: int) -> None:
    """A pause at noon: a stage whose 08:00 is today or earlier is skipped."""
    back = TODAY + dt.timedelta(days=days_away)

    assert first_reminder_stage(return_on=back, paused_on=TODAY, local_now=at(TODAY, 12)) == first


def test_a_stage_due_later_today_is_still_ahead() -> None:
    back = TODAY + dt.timedelta(days=7)

    assert first_reminder_stage(return_on=back, paused_on=TODAY, local_now=at(TODAY, 7)) == 7


def test_an_undated_pause_counts_from_when_it_began() -> None:
    assert first_reminder_stage(return_on=None, paused_on=TODAY, local_now=at(TODAY, 12)) == 30
    later = TODAY + dt.timedelta(days=40)
    assert first_reminder_stage(return_on=None, paused_on=TODAY, local_now=at(later, 12)) == 59


@pytest.mark.parametrize(("days_before", "latest"), [(8, None), (7, 7), (5, 7), (2, 3), (0, 0)])
def test_the_latest_due_stage_is_the_one_a_late_run_sends(
    days_before: int, latest: int | None
) -> None:
    back = TODAY + dt.timedelta(days=10)
    now = at(back - dt.timedelta(days=days_before), 9)

    assert latest_due_stage(return_on=back, paused_on=TODAY, local_now=now) == latest


@pytest.mark.parametrize(
    ("days_paused", "latest"), [(29, None), (30, 30), (58, 30), (59, 59), (61, 59)]
)
def test_an_undated_late_run_finds_the_latest_nudge(days_paused: int, latest: int | None) -> None:
    now = at(TODAY + dt.timedelta(days=days_paused), 9)

    assert latest_due_stage(return_on=None, paused_on=TODAY, local_now=now) == latest


@pytest.mark.parametrize(("days_after", "missed"), [(0, False), (1, True)])
def test_a_stage_is_missed_once_its_day_has_gone(days_after: int, missed: bool) -> None:
    back = TODAY + dt.timedelta(days=10)
    now = at(back - dt.timedelta(days=3 - days_after), 23, 59)

    assert stage_missed(3, return_on=back, paused_on=TODAY, local_now=now) is missed


@pytest.mark.parametrize("dated", [True, False])
def test_the_steps_invert(dated: bool) -> None:
    for offset in cadence(dated=dated):
        assert stage_before(stage_after(offset, dated=dated), dated=dated) == offset


def test_the_one_template_hears_how_far_away_the_return_is() -> None:
    """One template: a dated stage fills `daysUntilReturn` and `returnOn`."""
    from app.domain.messages import MessageContext, build_variables

    context = MessageContext(
        recipient_name="Ada",
        recipient_timezone="UTC",
        mentor_name="",
        mentee_name="",
        app_base_url="https://app.example",
        extras={"return_on": "2026-10-03", "stage": "3", "days_until_return": "3"},
    )

    built = build_variables(["daysUntilReturn", "returnOn", "daysPaused", "calendarUrl"], context)

    assert built == {
        "daysUntilReturn": "3",
        "returnOn": "Saturday 03 October 2026",
        "daysPaused": "",
        "calendarUrl": "https://app.example/calendar",
    }


def test_the_same_template_hears_how_long_an_undated_pause_has_run() -> None:
    from app.domain.messages import MessageContext, build_variables

    context = MessageContext(
        recipient_name="Ada",
        recipient_timezone="UTC",
        mentor_name="",
        mentee_name="",
        app_base_url="https://app.example",
        extras={"return_on": "", "stage": "30", "days_paused": "30"},
    )

    built = build_variables(["daysUntilReturn", "returnOn", "daysPaused"], context)

    assert built == {"daysUntilReturn": "", "returnOn": "", "daysPaused": "30"}

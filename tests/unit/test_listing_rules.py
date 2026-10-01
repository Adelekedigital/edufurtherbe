"""The return-date rule for a self-pause, without a database."""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.listing import (
    RETURN_REMINDER_OFFSETS,
    first_reminder_stage,
    return_on_problem,
    stage_after,
    stage_before,
)

TODAY = dt.date(2026, 10, 1)


@pytest.mark.parametrize("days", [0, -1, -30])
def test_today_or_earlier_is_refused(days: int) -> None:
    assert return_on_problem(TODAY + dt.timedelta(days=days), TODAY) is not None


def test_tomorrow_is_the_earliest_allowed() -> None:
    assert return_on_problem(TODAY + dt.timedelta(days=1), TODAY) is None


def test_not_sure_yet_is_always_allowed() -> None:
    assert return_on_problem(None, TODAY) is None


def test_the_stages_run_a_week_three_days_and_the_day() -> None:
    assert RETURN_REMINDER_OFFSETS == (7, 3, 0)


@pytest.mark.parametrize(
    ("days_away", "first"),
    [(10, 7), (8, 7), (7, 3), (5, 3), (3, 0), (2, 0), (1, 0)],
)
def test_the_first_stage_is_the_first_still_ahead(days_away: int, first: int) -> None:
    """A pause at noon: a stage whose 08:00 is today or earlier is skipped."""
    noon = dt.datetime.combine(TODAY, dt.time(12))

    assert first_reminder_stage(TODAY + dt.timedelta(days=days_away), noon) == first


def test_a_stage_due_later_today_is_still_ahead() -> None:
    early = dt.datetime.combine(TODAY, dt.time(7))

    assert first_reminder_stage(TODAY + dt.timedelta(days=7), early) == 7


def test_the_steps_invert() -> None:
    for offset in RETURN_REMINDER_OFFSETS:
        assert stage_before(stage_after(offset)) == offset


def test_the_one_template_hears_how_far_away_the_return_is() -> None:
    """One template on the whole cadence: the stage reaches it as a variable."""
    from app.domain.messages import MessageContext, build_variables

    context = MessageContext(
        recipient_name="Ada",
        recipient_timezone="UTC",
        mentor_name="",
        mentee_name="",
        app_base_url="https://app.example",
        extras={"return_on": "2026-10-03", "stage": "3"},
    )

    built = build_variables(["daysUntilReturn", "returnOn", "calendarUrl"], context)

    assert built == {
        "daysUntilReturn": "3",
        "returnOn": "Saturday 03 October 2026",
        "calendarUrl": "https://app.example/calendar",
    }

"""The return-date rule for a self-pause, without a database."""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.listing import return_on_problem

TODAY = dt.date(2026, 10, 1)


@pytest.mark.parametrize("days", [0, -1, -30])
def test_today_or_earlier_is_refused(days: int) -> None:
    assert return_on_problem(TODAY + dt.timedelta(days=days), TODAY) is not None


def test_tomorrow_is_the_earliest_allowed() -> None:
    assert return_on_problem(TODAY + dt.timedelta(days=1), TODAY) is None


def test_not_sure_yet_is_always_allowed() -> None:
    assert return_on_problem(None, TODAY) is None

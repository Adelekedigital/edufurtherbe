"""The configurable booking window can never exceed what the columns hold.

`BOOKING_WINDOW_CEILING` bounds `MAX_BOOKING_WINDOW_DAYS` and the write schemas;
the `booking_window_days_sane` CHECK on both tables bounds the columns. They are
two representations of one limit, so this pins them together (rule 8): raise one
without the other and this fails, rather than a write passing validation and
failing at the database as a 500.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import BOOKING_WINDOW_CEILING, Settings
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionTypeBookingConfig


@pytest.mark.parametrize("model", [MentorProfile, SessionTypeBookingConfig])
def test_the_column_check_matches_the_ceiling(model: type) -> None:
    (check,) = [
        constraint
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if str(constraint.name).endswith("booking_window_days_sane")
    ]

    assert f"BETWEEN 1 AND {BOOKING_WINDOW_CEILING}" in str(check.sqltext)


def test_a_maximum_above_the_ceiling_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_booking_window_days=BOOKING_WINDOW_CEILING + 1)
    assert Settings(_env_file=None, max_booking_window_days=BOOKING_WINDOW_CEILING)

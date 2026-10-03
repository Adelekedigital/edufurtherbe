"""The suggested-time clock (#339, decision 230): two hours held, nudged at ninety minutes."""

from __future__ import annotations

import datetime as dt

from app.domain.messages import MessageContext, build_variables
from app.domain.notifications import AUDIENCE, Audience, Notification
from app.domain.suggestions import (
    SUGGESTION_HOLD,
    SUGGESTION_REMINDER_KIND,
    held_until,
    suggestion_reminder_at,
)

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)


def test_a_suggestion_is_held_for_two_hours() -> None:
    assert dt.timedelta(hours=2) == SUGGESTION_HOLD
    assert held_until(NOW) == NOW + dt.timedelta(hours=2)


def test_the_reminder_is_thirty_minutes_before_the_hold_lapses() -> None:
    assert suggestion_reminder_at(held_until(NOW)) == NOW + dt.timedelta(minutes=90)


def test_the_reminder_kind_is_its_own() -> None:
    """Prefixed so no other reminder rule can fire it."""
    assert SUGGESTION_REMINDER_KIND == "h30"


def test_both_messages_go_to_the_mentee() -> None:
    assert AUDIENCE[Notification.SESSION_TIME_SUGGESTED] is Audience.MENTEE
    assert AUDIENCE[Notification.SESSION_SUGGESTION_REMINDER] is Audience.MENTEE


def test_the_offer_renders_in_the_mentees_zone() -> None:
    context = MessageContext(
        recipient_name="Mo",
        recipient_timezone="Africa/Lagos",
        mentor_name="Ada",
        mentee_name="Mo",
        extras={
            "suggested_starts_at": "2026-10-05T14:00:00+00:00",
            "held_until": "2026-10-03T14:00:00+00:00",
            "suggested_after": "declined",
        },
    )

    built = build_variables(
        ["suggestedDate", "suggestedTime", "holdUntilTime", "suggestedAfter"], context
    )

    assert built == {
        "suggestedDate": "Monday 05 October 2026",
        "suggestedTime": "15:00",
        "holdUntilTime": "15:00",
        "suggestedAfter": "declined",
    }

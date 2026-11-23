"""Which provider needs what, as a table rather than as inference.

The two flags are **independent**, and that is the whole reason this is a table:
a reader who assumes *needs a room* and *wants a conference* are opposites gets
Meet right and Daily wrong. Meet's link is a property of the calendar event;
Daily's room is its own object created before any calendar sees it.
"""

from __future__ import annotations

import pytest

from app.domain.enums import ConferencingProvider
from app.domain.meetings import call_exit_url, plan_for


def test_meet_asks_the_calendar_and_creates_no_room() -> None:
    """One call, not two. The link comes back on the event."""
    plan = plan_for(ConferencingProvider.GOOGLE_MEET)

    assert plan.wants_conference
    assert not plan.needs_room
    assert not plan.reuses_a_static_room


def test_daily_creates_a_room_and_must_not_ask_for_a_conference() -> None:
    """**The failure with no error message.** Requesting a Meet conference on a
    session held in Daily puts two links on the event, and the invitee clicks
    whichever the client renders first."""
    plan = plan_for(ConferencingProvider.DAILY)

    assert plan.needs_room
    assert not plan.wants_conference


def test_a_custom_venue_creates_nothing_at_all() -> None:
    """The mentor already typed the URL. Nothing mints it, and asking Google for
    a conference would add a second link beside the one they chose."""
    plan = plan_for(ConferencingProvider.CUSTOM)

    assert plan.reuses_a_static_room
    assert not plan.needs_room
    assert not plan.wants_conference


@pytest.mark.parametrize("provider", list(ConferencingProvider))
def test_exactly_one_source_of_a_url_per_provider(provider: ConferencingProvider) -> None:
    """**The invariant the whole table exists to hold.**

    A session has one `meeting_url`, so exactly one of the three has to produce
    it: the room, the conference, or the mentor. Two would mean a column with
    two candidate values and no rule for which wins; none would mean a confirmed
    session nobody can attend.

    Parametrised over the live vocabulary rather than the three named above, so
    adding a provider fails here instead of silently taking whichever branch its
    name happens to miss.
    """
    plan = plan_for(provider)

    sources = [plan.needs_room, plan.wants_conference, plan.reuses_a_static_room]
    assert sum(sources) == 1, f"{provider} has {sum(sources)} sources of a meeting URL"


def test_the_mentor_leaves_for_the_dashboard_and_the_mentee_for_the_session() -> None:
    base = "https://app.example.test/"
    assert call_exit_url(base, "s-1", is_mentor=True) == "https://app.example.test/dashboard"
    assert (
        call_exit_url(base, "s-1", is_mentor=False)
        == "https://app.example.test/sessions/s-1?from=call"
    )


def test_no_app_origin_means_no_exit_url() -> None:
    assert call_exit_url("", "s-1", is_mentor=True) is None
    assert call_exit_url("", "s-1", is_mentor=False) is None

"""Stand-ins for the room provider and the calendar, shared by the meeting,
door and presence suites. One copy each (non-negotiable #8); the `door`
fixture that wires them lives in this directory's `conftest.py`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.infra.clients.meetings import CalendarEvent, MeetingRoom, VenueUnavailableError


@dataclass
class FakeRooms:
    """Records what it was asked to make."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        self.calls.append({"name": name, "opens_at": opens_at, "closes_at": closes_at})
        return MeetingRoom(url="https://ef.daily.co/room", external_id="room-1")


#: The link the fake calendar's Meet comes back with.
FAKE_MEET = "https://meet.google.com/abc-defg-hij"


@dataclass
class FakeCalendar:
    """Records the events it was asked for, and every Meet patched onto one."""

    #: Set to make `add_conference` fail the way Google can.
    refuses: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    conferences: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def create_event(
        self,
        *,
        organiser_id: str,
        attendee_emails: Sequence[str],
        starts_at: dt.datetime,
        duration_minutes: int,
        summary: str,
        join_url: str | None,
    ) -> CalendarEvent:
        """The real adapter's signature, exactly: an argument it no longer takes
        fails here, where a `**kwargs` would have swallowed it."""
        self.calls.append(
            {
                "organiser_id": organiser_id,
                "attendee_emails": attendee_emails,
                "starts_at": starts_at,
                "duration_minutes": duration_minutes,
                "summary": summary,
                "join_url": join_url,
            }
        )
        return CalendarEvent(external_id="event-1")

    def cancel_event(self, external_id: str) -> None:
        self.cancelled.append(external_id)

    def add_conference(self, external_id: str, *, request_id: str) -> str:
        self.conferences.append({"external_id": external_id, "request_id": request_id})
        if self.refuses:
            raise VenueUnavailableError("google refused the conference")
        return FAKE_MEET


@dataclass
class FakeDoor(FakeRooms):
    """A room provider that also mints tokens, recording what it was asked."""

    tokens: list[dict[str, Any]] = field(default_factory=list)

    def token_for(self, **kwargs: Any) -> str:
        self.tokens.append(kwargs)
        return "minted-token"

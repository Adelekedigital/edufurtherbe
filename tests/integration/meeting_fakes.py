"""Stand-ins for the room provider and the calendar, shared by the meeting,
door and presence suites. One copy each (non-negotiable #8); the `door`
fixture that wires them lives in this directory's `conftest.py`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.infra.clients.meetings import CalendarEvent, MeetingRoom


@dataclass
class FakeRooms:
    """Records what it was asked to make."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        self.calls.append({"name": name, "opens_at": opens_at, "closes_at": closes_at})
        return MeetingRoom(url="https://ef.daily.co/room", external_id="room-1")


@dataclass
class FakeCalendar:
    """Records whether a conference was requested, which is the whole point."""

    hands_back_a_link: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create_event(self, **kwargs: Any) -> CalendarEvent:
        self.calls.append(kwargs)
        return CalendarEvent(
            external_id="event-1",
            meeting_url=("https://meet.google.com/abc" if self.hands_back_a_link else None),
        )


@dataclass
class FakeDoor(FakeRooms):
    """A room provider that also mints tokens, recording what it was asked."""

    tokens: list[dict[str, Any]] = field(default_factory=list)

    def token_for(self, **kwargs: Any) -> str:
        self.tokens.append(kwargs)
        return "minted-token"

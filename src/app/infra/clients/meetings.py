"""Making a room, and putting it on a calendar. Neither is wired.

Two adapters and a null one apiece, following ``hipolabs`` and the notification
clients beside this file: a concrete class per source, structurally
interchangeable, no ``Protocol`` declared.

**What ships is the shape.** The orchestration above these — which provider,
whether to ask for a conference, which columns to write — is ours and is tested
against the null adapters. The calls themselves are the integration phase.

**The Google side is blocked on something that does not exist**:
``calendar_connections`` was deferred under settled decisions #21 and #26, and
ADR 0012 has not settled the OAuth arrangement its columns would encode. So
there is nowhere to read a mentor's token from, and no calendar event can be
created for anybody until that lands. The null adapter is not a placeholder for
laziness; it is the honest state of the system.

**The Daily side is blocked on a measurement**, not a decision.
`scripts/daily_spike.py` asks six questions whose answers shape this adapter —
whether a private room refuses an untokened URL, whether ``nbf`` is enforced,
whether the meeting record carries per-participant join *and* leave, and how
long after the call it appears. Building against the documentation before those
are answered is what the calendar spike exists to stop us doing.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError
from app.domain.enums import ConferencingProvider

__all__ = [
    "CalendarEvent",
    "DailyRooms",
    "GoogleCalendar",
    "MeetingRoom",
    "NullCalendar",
    "NullRooms",
    "VenueUnavailableError",
]

logger = logging.getLogger(__name__)


class VenueUnavailableError(AppError):
    """A room or an event could not be created.

    Deliberately **not** mapped to a status code yet. A booking that succeeds
    without a link is recoverable — the link can be minted later and the session
    still exists — where a booking refused because a third party was slow is
    not. Which of those we want is a decision for the release that wires this.
    """


@dataclass(frozen=True, slots=True)
class MeetingRoom:
    """A place to meet, and the provider's handle on it."""

    url: str
    #: `sessions.external_room_id`. Null for a venue we did not create — a
    #: custom URL has no provider-side object, and a Meet conference is
    #: identified by the calendar event rather than by a room.
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """What the calendar gave back.

    ``meeting_url`` is populated **only** when the event was asked for a
    conference — that is Meet's link, arriving by the one path that produces it.
    For every other venue the URL was already known and the event merely carries
    it, so this stays null and the caller keeps what it had.
    """

    external_id: str
    meeting_url: str | None = None


class NullRooms:
    """Creates nothing and says so. The default."""

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        raise VenueUnavailableError(
            f"no room provider configured — cannot create {name!r} "
            f"for {opens_at.isoformat()}-{closes_at.isoformat()}"
        )


class DailyRooms:
    """Daily, and it is not built.

    Raises rather than returning a plausible object, because a room URL that
    leads nowhere is worse than a session with no link at all: the mentee
    arrives, finds nothing, and has no reason to think anything is wrong with
    the platform rather than with them.

    What the spike decides before this is written: whether ``privacy: private``
    refuses an untokened URL — the whole withheld-link design rests on it — and
    whether a token's ``nbf`` is enforced or advisory.
    """

    def __init__(self, api_key: str, client: Any = None) -> None:
        self._api_key = api_key
        self._client = client

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        del name, opens_at, closes_at
        raise NotImplementedError(
            "the Daily adapter is not built — run scripts/daily_spike.py first, "
            "and see docs/daily-spike-guide.md for what each answer changes"
        )


class NullCalendar:
    """Writes no event and returns nothing. The default, and the honest one.

    Returning ``None`` rather than raising, and the asymmetry with `NullRooms`
    is deliberate: a session with no calendar event is the state every session
    is in today and nothing is broken by it, where a session with no *room* is a
    session nobody can attend.
    """

    def create_event(
        self,
        *,
        organiser_id: str,
        attendee_email: str,
        starts_at: dt.datetime,
        duration_minutes: int,
        summary: str,
        wants_conference: bool,
        meeting_url: str | None,
    ) -> CalendarEvent | None:
        # Every argument is named and unused on purpose: the signature is the
        # contract the orchestration is written against, and trimming it to what
        # a null implementation happens to touch would make the real adapter's
        # arrival a signature change at every call site.
        del attendee_email, duration_minutes, summary, meeting_url
        logger.info(
            "no calendar configured; not writing an event for %s at %s (conference requested: %s)",
            organiser_id,
            starts_at.isoformat(),
            wants_conference,
        )
        return None


class GoogleCalendar:
    """Google Calendar, and it is not built.

    **Blocked on `calendar_connections`**, which does not exist: there is
    nowhere to hold the mentor's token, and only mentors connect — the mentee
    receives an invitation and completes no OAuth flow (settled decision #15,
    proved by the spike's Q1).

    Two things this adapter must get right, both measured rather than assumed:

    ``conferenceDataVersion=1`` is mandatory when ``wants_conference``. Without
    it the API accepts the write and **silently drops the conference**, which is
    indistinguishable from a permissions refusal unless you read the response.

    Free/busy is **eventually consistent**. A write followed immediately by a
    free/busy read returns empty, so conflict detection happens before the write
    or tolerates the lag — which is why `POST /sessions` checks the slot grid
    first and lets the exclusion constraint be the authority.
    """

    def __init__(self, credentials: Any = None, client: Any = None) -> None:
        self._credentials = credentials
        self._client = client

    def create_event(
        self,
        *,
        organiser_id: str,
        attendee_email: str,
        starts_at: dt.datetime,
        duration_minutes: int,
        summary: str,
        wants_conference: bool,
        meeting_url: str | None,
    ) -> CalendarEvent | None:
        # Named and unused for the same reason as `NullCalendar` — see there.
        del organiser_id, attendee_email, starts_at, duration_minutes
        del summary, wants_conference, meeting_url
        raise NotImplementedError(
            "the Google Calendar adapter is not built — it needs calendar_connections "
            "and ADR 0012's OAuth arrangement settled first"
        )


def room_name(session_id: str, provider: ConferencingProvider) -> str:
    """A room name that is unique per session and says nothing about anybody.

    **The session id and nothing else.** A name carrying a mentor's handle or a
    mentee's would put both in a URL that ends up in calendar entries, browser
    history and screenshots — and a room name is not access-controlled.
    """
    return f"ef-{provider}-{session_id}"

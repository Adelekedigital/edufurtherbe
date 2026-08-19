"""Making a room, and putting it on a calendar. Neither is wired.

Two adapters and a null one apiece, following ``hipolabs`` and the notification
clients beside this file: a concrete class per source, structurally
interchangeable, no ``Protocol`` declared.

**What ships is the shape.** The orchestration above these — which provider,
whether to ask for a conference, which columns to write — is ours and is tested
against the null adapters. The calls themselves are the integration phase.

**The Google side is blocked on one refresh token, not on a table.** An earlier
version of this docstring said it needed ``calendar_connections`` — that nothing
could create an event for anybody until a per-mentor token store existed. That
was wrong, and wrong in the expensive direction: acting on it would have meant
building a table to unblock something that never needed it.

The calendar is **EduFurther's own Google account**, not each mentor's. The
platform account creates the event and invites both parties, which the spike
measured working end to end on a consumer Gmail: invitations delivered, a Meet
link minted, and both guests joining a room the creator never entered.

``calendar_connections`` is a real and separate piece of work — it records a
mentor's ``calendar.freebusy`` grant, which buys *conflict detection* against
their existing calendar. That is a later gap, and building it now to unblock this
adapter would be building the wrong thing.

**The Daily side was blocked on a measurement, and most of it is now answered.**
`docs/daily-spike-guide.md` has the run. The record carries per-participant
``join_time`` and ``duration`` — two ends, so intervals, so co-presence — the
``user_id`` we mint round-trips verbatim, and there is **no lag at all**: the
record is readable during the call, which is the opposite of the Google
free/busy result.

The trap it did surface is ``ongoing``. A record read mid-call carries partial
durations, and the join window shuts fifteen minutes into a session that runs
thirty to ninety — so attendance and *co-presence* are two questions answerable
at two different times, and this adapter's reader has to know which it is
serving.

**Q1 and Q2 are still open**, and Q1 is load-bearing: whether ``privacy:
private`` refuses a visitor holding the URL and no token is the whole basis for
not publishing the link.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import httpx

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

API_BASE = "https://api.daily.co/v1"

#: Long enough for a slow third party, short enough that a booking does not hang
#: on one. A room that fails to mint is recoverable; a request that never
#: answers is not.
TIMEOUT = httpx.Timeout(15.0)


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

    Rooms are created ``private`` unconditionally — see the note on the spike
    below.

    Raises rather than returning a plausible object, because a room URL that
    leads nowhere is worse than a session with no link at all: the mentee
    arrives, finds nothing, and has no reason to think anything is wrong with
    the platform rather than with them.

    What the spike has since settled: the room accepts ``nbf`` and ``exp`` in
    ``properties`` and reflects them in ``config``, a meeting token carries
    ``room_name``, ``user_name``, ``user_id``, ``is_owner``, ``nbf`` and
    ``exp``, and the joined URL is ``<room url>?t=<token>``.

    **Every session room is private, always.** Not a per-session judgement and
    not a setting — there is no case for a public one, and the URL reaching a
    participant only through the platform is what makes the join a thing we can
    record. Whether Daily *enforces* it is unconfirmed; if it did not, that
    would be a defect to raise with them rather than a choice to revisit.

    Still open: whether ``nbf`` is enforced at the door or merely recorded. That
    one is a genuine fork — enforced, early joining is impossible here and only
    discouraged on Meet; advisory, both providers need the same UI copy.

    **A webhook belongs beside this, not instead of it.** ``GET /meetings``
    settles a finished session and the hourly sweep loses nobody, because the
    record accumulates. It cannot make a *waiting* screen true: the arrival a
    participant sees today is our own Join press, which records an intention, so
    somebody who presses it and never reaches the room leaves the other party
    looking at a lie. ``participant.joined`` and ``participant.left`` are what
    fix that, and they bring a public endpoint, signature verification and
    tolerance for retries and out-of-order delivery with them.
    """

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT,
        )

    def create(self, *, name: str, opens_at: dt.datetime, closes_at: dt.datetime) -> MeetingRoom:
        """A private room that exists between the two instants and no longer.

        ``nbf`` and ``exp`` are the room's own gate. The spike confirmed Daily
        accepts both and reflects them in ``config``; whether it *enforces*
        ``nbf`` at the door is the one question still open, and the answer
        decides whether "the room is shut until five minutes before" is a
        promise or a request. Either way setting them costs nothing and is the
        only mechanism available.

        **The returned URL is not a door.** A private room refuses anybody
        without a token, so this is the room's address and `token_for` is what
        opens it — which is the whole point of not publishing the link.
        """
        room = self._post(
            "/rooms",
            {
                "name": name,
                "privacy": "private",
                "properties": {
                    "nbf": int(opens_at.timestamp()),
                    "exp": int(closes_at.timestamp()),
                    # So a refused join is visibly refused rather than a blank
                    # page the participant has to interpret.
                    "enable_prejoin_ui": True,
                },
            },
        )
        return MeetingRoom(url=str(room["url"]), external_id=str(room.get("name") or name))

    def token_for(
        self,
        *,
        room: str,
        user_id: str,
        user_name: str,
        is_owner: bool,
        opens_at: dt.datetime,
        closes_at: dt.datetime,
    ) -> str:
        """A short-lived credential for one person, minted when they ask.

        **Never stored.** A meeting token is a bearer credential for a live
        room, so keeping one on the session would put two of them in the
        database and in every backup — and they would outlive the reason they
        were made. Minting on demand costs one request at the moment somebody
        is already waiting for a page.

        ``user_id`` is our own `users.id`, which the spike measured coming back
        verbatim on the meeting record. That is what makes attendance
        attributable without a second lookup table.
        """
        minted = self._post(
            "/meeting-tokens",
            {
                "properties": {
                    "room_name": room,
                    "user_id": user_id,
                    "user_name": user_name,
                    # The mentor hosts. On Daily an owner can admit, mute and
                    # end — which is the mentor's role and not the mentee's.
                    "is_owner": is_owner,
                    "nbf": int(opens_at.timestamp()),
                    "exp": int(closes_at.timestamp()),
                }
            },
        )
        return str(minted["token"])

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """One place where a Daily failure becomes ours.

        Every network error and every non-2xx becomes `VenueUnavailableError`,
        because the caller's decision is the same for all of them: carry on
        without a venue rather than fail the booking. Letting `httpx` raise
        would make a third party's bad afternoon into a 500 on a request that
        had already done the thing the user asked for.
        """
        try:
            response = self._client.post(path, json=body)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VenueUnavailableError(f"daily {path} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise VenueUnavailableError(f"daily {path} returned {type(payload).__name__}")
        return payload


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

    **Blocked on a refresh token in configuration**, not on a table. The event is
    created by the platform's own Google account and both parties are invited as
    guests — neither completes an OAuth flow, which is settled decision #15 held
    more strongly than it was written, and is what the spike's Q1 measured.

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
            "the Google Calendar adapter is not built — it needs the platform "
            "account's refresh token in configuration"
        )


def room_name(session_id: str, provider: ConferencingProvider) -> str:
    """A room name that is unique per session and says nothing about anybody.

    **The session id and nothing else.** A name carrying a mentor's handle or a
    mentee's would put both in a URL that ends up in calendar entries, browser
    history and screenshots — and a room name is not access-controlled.
    """
    return f"ef-{provider}-{session_id}"

"""Making a room, and putting it on a calendar. Neither is wired.

Two adapters and a null one apiece, following ``hipolabs`` and the notification
clients beside this file: a concrete class per source, structurally
interchangeable, no ``Protocol`` declared.

**The Google side is now wired; the Daily side is the shape only.** Events are
written and removed against the platform's account, a mentor's consent is
exchanged and their free/busy read, and each is tested by asserting the
*request* rather than a stubbed return — which is where every defect in this
file has actually been. `DailyRooms` still ships as a shape whose orchestration
is tested against the null adapters.

**Two Google grants, and they are not the same grant.** Writing a session's
event uses **EduFurther's own account** — one stored refresh token in
configuration, no table, no consent from anybody (ADR 0012). Reading a mentor's
busy hours uses **that mentor's** ``calendar.freebusy`` grant, which is a row in
``calendar_connections`` and the consent flow beside it.

An earlier version of this docstring claimed the event writer was blocked on
that table. It never was, and acting on the belief would have meant building a
table to unblock something that needed nothing from it. The two are recorded
apart here because conflating them is a mistake this file has already made
once.

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

**Q1 and Q2 are answered, and both the way the design assumed.** A private
room refuses an untokened visitor — *"You are not allowed to join this
meeting"* — and ``nbf`` is enforced at the door, not merely recorded: *"This
meeting is not ready yet"*. Two different refusals, which is how the run is
known to have tested two things rather than one thing twice.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.errors import UpstreamError
from app.domain.availability import UtcInterval
from app.domain.enums import ConferencingProvider

__all__ = [
    "FREEBUSY_SCOPE",
    "GOOGLE_CALENDAR_API",
    "MENTOR_CALENDAR",
    "CalendarAccessRevokedError",
    "CalendarEvent",
    "DailyRooms",
    "GoogleCalendar",
    "MeetingRoom",
    "NullCalendar",
    "NullRooms",
    "VenueUnavailableError",
    "access_token",
    "consent_url",
    "exchange_code",
    "free_busy",
]

logger = logging.getLogger(__name__)

API_BASE = "https://api.daily.co/v1"

GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
#: Google's public OAuth endpoint. Flagged as a credential by the secret
#: scanner because of the name; it is a URL every Google client uses.
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105

#: Long enough for a slow third party, short enough that a booking does not hang
#: on one. A room that fails to mint is recoverable; a request that never
#: answers is not.
TIMEOUT = httpx.Timeout(15.0)


#: A room or an event could not be created.
#:
#: **Now `core.UpstreamError`**, which is what makes it mappable: `api` may not
#: import `infra`, so while this class was defined here the transport layer
#: could not name it and every escape became a 500. The old name stays because
#: it reads better at the call sites — "the venue is unavailable" is what a room
#: provider failing actually means.
#:
#: The status is **502**, decided by the release that wired the consent
#: callback, which is the first path that lets one reach a client. Booking still
#: catches it and continues without a link: a session with no link is
#: recoverable, and refusing the booking because a third party was slow is not.
VenueUnavailableError = UpstreamError


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
    record. **Daily enforces it**, measured rather than assumed: an untokened
    visitor holding the room URL is refused with *"You are not allowed to join
    this meeting"*. So "nobody can join without the link we gave you" is a
    promise this platform can make rather than merely hope for.

    **``nbf`` is enforced at the door too** — *"This meeting is not ready yet"*
    — which settles the fork the spike was run for. Early joining is
    *impossible* on Daily and only *discouraged* on Meet, so the two venues are
    not equivalent and their copy cannot be either.

    That asymmetry is the useful part. A Daily session has two layers: the link
    is withheld until the join window **and** the door is shut. A Meet session
    has one, because a Meet link admits anybody holding it at any time — which
    is exactly why links are withheld rather than emailed in advance.

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

        ``nbf`` and ``exp`` are the room's own gate, and Daily **enforces**
        them: a tokened participant arriving early is refused with *"This
        meeting is not ready yet"*. So "the room is shut until five minutes
        before" is a promise rather than a request.

        Both ``nbf``s are set together — one on the room, one on the token — so
        the measurement covers the pair. Which of the two does the refusing is
        untested and does not matter while both are set.

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

    def cancel_event(self, external_id: str) -> None:
        logger.info("no calendar configured; not removing event %s", external_id)


class GoogleCalendar:
    """The platform's own Google account, creating the event and inviting both.

    **Plain REST rather than the Google SDK.** `google-api-python-client` brings
    a discovery mechanism and a large dependency tree to do what two POSTs do —
    exchange a refresh token, insert an event — and the spike proved the shape
    of both. `DailyRooms` beside it works the same way, so one reader learns one
    pattern.

    **Two things measured rather than assumed, and both are traps.**

    ``conferenceDataVersion=1`` is mandatory when a Meet link is wanted. Without
    it the API accepts the write and **silently drops** ``conferenceData``,
    which is indistinguishable from a permissions refusal unless you read the
    response — so this asserts the link came back rather than trusting a 200.

    Free/busy is **eventually consistent**: a write followed immediately by a
    read returns empty. Nothing here reads it, and that is why `POST /sessions`
    checks the slot grid before writing and lets the exclusion constraint be the
    authority.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
        client: httpx.Client | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._calendar_id = calendar_id
        self._client = client or httpx.Client(timeout=TIMEOUT)
        self._token: str | None = None
        self._token_expires: dt.datetime | None = None

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
        """One event, both parties invited, and a Meet link only when asked.

        **`wants_conference` is the line that matters.** Requesting one for a
        session held in Daily would put a second link on the event, and the
        invitee clicks whichever the client renders first — a failure that
        errors nowhere and surfaces when somebody joins an empty room.

        The venue's own URL goes in the description rather than the location,
        because a Daily room is not a place and a client rendering `location`
        as a map pin would be confidently wrong.
        """
        ends_at = starts_at + dt.timedelta(minutes=duration_minutes)
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": starts_at.isoformat()},
            "end": {"dateTime": ends_at.isoformat()},
            # Both parties, and neither completes an OAuth flow — settled
            # decision #15, which the spike's Q1 measured working on a consumer
            # Gmail account.
            "attendees": [{"email": attendee_email}],
            "extendedProperties": {"private": {"edufurther_session_id": organiser_id}},
        }
        params = {"sendUpdates": "all"}
        if wants_conference:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": organiser_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            # Measured, not assumed: without this the write succeeds and the
            # conference is dropped in silence.
            params["conferenceDataVersion"] = "1"
        elif meeting_url:
            body["description"] = f"Join here: {meeting_url}"

        event = self._call(
            "POST",
            f"/calendars/{self._calendar_id}/events",
            params=params,
            json=body,
        )
        link = event.get("hangoutLink")
        if wants_conference and not link:
            # The silent drop, caught. A 200 with no link means the conference
            # was refused or the version parameter was lost, and returning the
            # event anyway would store an id for a meeting with nowhere to go.
            raise VenueUnavailableError(
                "Google accepted the event and returned no Meet link — "
                "conferenceDataVersion was dropped or the scope is missing"
            )
        return CalendarEvent(external_id=str(event["id"]), meeting_url=link)

    def cancel_event(self, external_id: str) -> None:
        """Remove the event, so a called-off session leaves both calendars.

        **Deleting rather than marking cancelled.** Google's `status:
        "cancelled"` leaves a struck-through entry in the invitee's calendar,
        which is arguably more informative and is also a thing they cannot get
        rid of.

        **`sendUpdates=all`, matching `create_event`.** These are one pair and
        were briefly not: the invitation notified and the cancellation did not,
        so a meeting a mentee had accepted simply vanished from their calendar
        with nothing said. Letting Google announce one end of a booking and not
        the other is the worst of both arrangements — a duplicate message is
        noise, where a silent disappearance is a mentee turning up to a session
        that is not happening.

        The platform's own notification is not an argument against this. It is
        equally an argument against notifying on the invitation, and that is not
        what the code does; whichever way it goes, both ends should agree.

        A `410` is success: the event is already gone, which is the state this
        method exists to reach.
        """
        try:
            self._call(
                "DELETE",
                f"/calendars/{self._calendar_id}/events/{external_id}",
                params={"sendUpdates": "all"},
            )
        except VenueUnavailableError as exc:
            if "410" in str(exc) or "404" in str(exc):
                return
            raise

    def _access_token(self) -> str:
        """A cached access token, refreshed when it is close to expiring.

        **Cached on the instance with a minute of headroom.** Google's tokens
        last an hour, and a refresh per call would be a second round trip on
        every booking to re-acquire something still valid. The headroom is
        because a token that expires between the check and the call fails in a
        way that looks like a permissions problem.
        """
        now = dt.datetime.now(dt.UTC)
        if self._token and self._token_expires and now < self._token_expires:
            return self._token

        self._token, lifetime = access_token(
            client_id=self._client_id,
            client_secret=self._client_secret,
            refresh_token=self._refresh_token,
            client=self._client,
        )
        self._token_expires = now + dt.timedelta(seconds=lifetime - 60)
        return self._token

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One place where a Google failure becomes ours.

        Every network error and every non-2xx becomes `VenueUnavailableError`,
        for the reason `DailyRooms` gives: the caller's decision is the same for
        all of them, and a third party's bad afternoon must not become a 500 on
        a request that has already done what the user asked.
        """
        try:
            response = self._client.request(
                method,
                f"{GOOGLE_CALENDAR_API}{path}",
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self._access_token()}"},
            )
            response.raise_for_status()
            if not response.content:
                return {}
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VenueUnavailableError(f"google {method} {path} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise VenueUnavailableError(f"google {path} returned {type(payload).__name__}")
        return payload


class CalendarAccessRevokedError(UpstreamError):
    """The mentor's grant is dead, and retrying will not revive it.

    **Separated from every other upstream failure because the response differs.**
    A timeout, a 503 or a rate limit means *ask again later* and the connection
    is fine; `invalid_grant` means the mentor revoked us in their Google
    settings, or the token aged out unused, and every future call fails
    identically. Treating the two alike would either re-consent a mentor over a
    blip, or leave a dead connection retrying silently forever.

    Google says this the same way in both cases — a `400` carrying
    ``"error": "invalid_grant"`` — so the distinction is made here, at the one
    place that can see the body.
    """


def access_token(
    *, client_id: str, client_secret: str, refresh_token: str, client: httpx.Client
) -> tuple[str, int]:
    """Exchange a refresh token for an access token and its lifetime.

    **One implementation, two callers**: the platform's own calendar refreshes
    its single configured token, and a mentor's free/busy read refreshes theirs.
    Non-negotiable #8 — a second copy of this is a second place for the grant
    type or the parameter names to be wrong, and the copy that drifts is the one
    nobody exercises.

    Raises :class:`CalendarAccessRevokedError` for `invalid_grant`, so a caller can
    tell a dead grant from a bad afternoon.
    """
    try:
        response = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    except httpx.HTTPError as exc:
        raise VenueUnavailableError(f"google token refresh failed: {exc}") from exc

    if response.status_code == httpx.codes.BAD_REQUEST and "invalid_grant" in response.text:
        raise CalendarAccessRevokedError("the grant was revoked or has expired")

    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise VenueUnavailableError(f"google token refresh failed: {exc}") from exc

    return str(payload["access_token"]), int(payload.get("expires_in", 3600))


#: The one scope a mentor is asked for. ADR 0012 narrowed the ask to this
#: deliberately: it is the least Google offers, and the consent screen then says
#: exactly one thing — *"View your availability in your calendars."*
#:
#: **Not `calendar.app.created`**, which is the platform account's own grant and
#: is configuration rather than consent. Conflating the two is what made an
#: earlier docstring claim the event writer needed a per-mentor table.
FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def consent_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Where to send a mentor to grant free/busy access.

    ``access_type=offline`` with ``prompt=consent`` because we need a **refresh**
    token and Google issues one only on a fresh grant — a mentor who has
    consented before otherwise comes back with an access token that dies in an
    hour, and the connection appears to work until it silently does not.

    ``include_granted_scopes`` is deliberately absent: it would let a previous
    grant widen this one, and the whole point of the narrow ask is that the
    consent screen says exactly what it says.
    """
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": FREEBUSY_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTH_URL}?{query}"


#: The mentor's own calendar, always. **Never `google_calendar_id`** — that
#: setting names a calendar on *EduFurther's* account, and pointing a mentor's
#: free/busy read at it would subtract the platform's own bookings from every
#: mentor's availability. The two settings look interchangeable and are not.
MENTOR_CALENDAR = "primary"


def free_busy(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    start: dt.datetime,
    end: dt.datetime,
    client: httpx.Client | None = None,
) -> tuple[UtcInterval, ...]:
    """When this mentor is busy in their own calendar, over ``[start, end)``.

    **One request for the whole span, not one per day.** `freeBusy` takes a
    range, and a call per day would turn a 56-day grid render into 56 round
    trips against a third party on an endpoint that takes no token.

    **Times, never contents.** The scope grants exactly this: the response is
    opaque intervals, so nothing here can learn what a mentor is doing — which
    is what made the narrow ask defensible to them in the first place.

    Returns intervals for :func:`app.domain.availability.bookable` to subtract.
    An empty tuple means *free*, and is not the same as a failed read — the
    caller distinguishes those, because this raises rather than returning empty
    on failure.
    """
    http = client or httpx.Client(timeout=TIMEOUT)
    token, _ = access_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        client=http,
    )
    try:
        response = http.post(
            f"{GOOGLE_CALENDAR_API}/freeBusy",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "items": [{"id": MENTOR_CALENDAR}],
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise VenueUnavailableError(f"google freeBusy failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise VenueUnavailableError(f"google freeBusy returned {type(payload).__name__}")

    calendar = payload.get("calendars", {}).get(MENTOR_CALENDAR, {})
    # **A per-calendar error is a failure, not an empty calendar.** `freeBusy`
    # answers 200 with the trouble reported inside the body — a revoked grant
    # arrives here as `{"errors": [{"reason": "notFound"}]}` rather than as a
    # status code, and reading past it would report a mentor with a full diary
    # as completely free.
    if calendar.get("errors"):
        raise VenueUnavailableError(f"google freeBusy refused the calendar: {calendar['errors']}")

    return tuple(
        UtcInterval(
            dt.datetime.fromisoformat(period["start"]).astimezone(dt.UTC),
            dt.datetime.fromisoformat(period["end"]).astimezone(dt.UTC),
        )
        for period in calendar.get("busy", [])
    )


def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Turn a consent code into a refresh token, or raise.

    **Refuses a response with no refresh token**, which is the failure that
    otherwise ships silently: Google returns 200 with only an access token when
    the grant was not fresh, and storing that gives a connection that works for
    an hour and then stops with no error anybody saw.
    """
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = http.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise VenueUnavailableError(f"google refused the consent code: {exc}") from exc

    if not isinstance(payload, dict) or not payload.get("refresh_token"):
        raise VenueUnavailableError(
            "Google returned no refresh token — the grant was not fresh, so "
            "access_type=offline and prompt=consent are both required"
        )
    return payload


def room_name(session_id: str, provider: ConferencingProvider) -> str:
    """A room name that is unique per session and says nothing about anybody.

    **The session id and nothing else.** A name carrying a mentor's handle or a
    mentee's would put both in a URL that ends up in calendar entries, browser
    history and screenshots — and a room name is not access-controlled.
    """
    return f"ef-{provider}-{session_id}"

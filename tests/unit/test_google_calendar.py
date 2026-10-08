"""What Google is actually asked, and what happens when it answers oddly.

**The whole reason this is a unit suite against a transport.** The Google side
of this codebase has one recorded trap and it is invisible from the return
value: without `conferenceDataVersion=1` the API **accepts the write and
silently drops the conference**, which `docs/calendar-spike-guide.md` records as
indistinguishable from a permissions refusal if you only read the response. A
test asserting the returned event would pass through it.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from app.infra.clients.meetings import GoogleCalendar, VenueUnavailableError

STARTS = dt.datetime(2026, 8, 25, 14, 0, tzinfo=dt.UTC)

#: Stand-ins for the OAuth pair. Named rather than inline because the secret
#: scanner flags a literal in that position — correctly, since a real one
#: here would be a credential committed to the repository.
FAKE_SECRET = "not-a-real-client-secret"  # noqa: S105
FAKE_REFRESH = "not-a-real-refresh-token"


def calendar(
    handler: Any, *, calendar_id: str = "primary"
) -> tuple[GoogleCalendar, list[httpx.Request]]:
    """An adapter whose calls are captured, with the token exchange stubbed."""
    seen: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(route))
    api = GoogleCalendar(
        client_id="cid",
        client_secret=FAKE_SECRET,
        refresh_token=FAKE_REFRESH,
        calendar_id=calendar_id,
        client=client,
    )
    return api, seen


def created(**overrides: Any) -> Any:
    body = {"id": "evt_1", **overrides}
    return lambda _: httpx.Response(200, json=body)


MENTEE = "mentee@example.test"
MENTOR = "mentor@example.test"
SESSION_PAGE = "https://app.example.test/sessions/sess-1"


def insert(
    api: GoogleCalendar,
    *,
    wants_conference: bool,
    join_url: str | None = SESSION_PAGE,
    guests: tuple[str, ...] = (MENTEE, MENTOR),
) -> Any:
    return api.create_event(
        organiser_id="sess-1",
        attendee_emails=guests,
        starts_at=STARTS,
        duration_minutes=60,
        summary="EduFurther session",
        wants_conference=wants_conference,
        join_url=join_url,
    )


# --------------------------------------------------------------------------
# Creating the event
# --------------------------------------------------------------------------


def test_a_meet_session_asks_for_a_conference_with_the_version_parameter() -> None:
    """**The trap, asserted as a query parameter.** Without
    `conferenceDataVersion=1` Google accepts the write and drops the conference,
    and nothing in the response says so."""
    api, seen = calendar(created(hangoutLink="https://meet.google.com/abc"))

    event = insert(api, wants_conference=True)

    (request,) = seen
    assert request.url.params["conferenceDataVersion"] == "1"
    assert json.loads(request.content)["conferenceData"]["createRequest"]
    assert event is not None
    assert event.meeting_url == "https://meet.google.com/abc"


def test_a_daily_session_does_not_ask_for_one() -> None:
    """**Two links on one event is the failure with no error message.** The
    invitee clicks whichever the client renders first, and nobody finds out
    until somebody joins an empty room."""
    api, seen = calendar(created())

    event = insert(api, wants_conference=False)

    (request,) = seen
    body = json.loads(request.content)
    assert "conferenceDataVersion" not in request.url.params
    assert "conferenceData" not in body
    assert event is not None
    assert event.meeting_url is None, "the venue's own URL is not Google's to return"


@pytest.mark.parametrize("wants_conference", [False, True])
def test_every_invite_links_to_the_session_page(wants_conference: bool) -> None:
    """**The session page, for every venue (#389).** It is where Join is pressed
    and recorded, and the page redirects whatever later happens to the session,
    so it is safe on an invite that outlives a cancellation. A Daily room's own
    URL was written here before, and a bare room URL is *refused* by Daily
    without a token (`docs/daily-spike-guide.md` Q1): every Daily invite sent
    linked to a room that turned its guest away."""
    api, seen = calendar(created(hangoutLink="https://meet.google.com/abc"))

    insert(api, wants_conference=wants_conference)

    assert SESSION_PAGE in json.loads(seen[0].content).get("description", "")


def test_with_no_session_page_the_event_is_still_written_without_a_link() -> None:
    """No `APP_BASE_URL` means no link to give. The event still goes out, so both
    parties have the time in their calendars; a bare path or a venue URL would
    each be worse than nothing."""
    api, seen = calendar(created())

    event = insert(api, wants_conference=False, join_url=None)

    assert "description" not in json.loads(seen[0].content)
    assert event is not None


def test_a_silently_dropped_conference_is_caught() -> None:
    """**A 200 with no link is a failure**, and the one this suite exists for.
    Returning the event anyway would store an id for a meeting with nowhere to
    go, and the session would look provisioned."""
    api, _ = calendar(created())

    with pytest.raises(VenueUnavailableError) as raised:
        insert(api, wants_conference=True)

    assert "conferenceDataVersion" in str(raised.value)


def test_both_parties_are_invited_and_the_session_is_recorded_on_the_event() -> None:
    """**Mentee and mentor, both guests of the platform's account** (#389).

    This test used to assert the mentee *alone*, and so pinned the defect: the
    adapter's own docstring and ADR 0012 both said both parties are invited, and
    for seven weeks the gate defended a list of one. The guest list matters
    beyond the calendar entry: Meet admits without knocking only the addresses
    on the invitation (ADR 0012 §4), so an uninvited mentor knocks on a call
    whose only admitter, the platform account, is never in it.

    The session id rides along so an event can be traced back without a database.
    """
    api, seen = calendar(created(hangoutLink="https://meet.google.com/abc"))

    insert(api, wants_conference=True)

    body = json.loads(seen[0].content)
    assert body["attendees"] == [{"email": MENTEE}, {"email": MENTOR}]
    assert body["extendedProperties"]["private"]["edufurther_session_id"] == "sess-1"


def test_guests_cannot_see_each_other_or_add_anyone() -> None:
    """**Inviting both parties must not introduce them** (security review, #389).

    Google's defaults are `guestsCanSeeOtherGuests: true` and
    `guestsCanInviteOthers: true`. The first would show each party the other's
    personal address in the event and the invitation, where the API itself
    withholds it. The second would let either party add a third person, who on
    Meet joins without knocking (ADR 0012 §4). Owner, 2026-10-08: both off.
    """
    api, seen = calendar(created(hangoutLink="https://meet.google.com/abc"))

    insert(api, wants_conference=True)

    body = json.loads(seen[0].content)
    # `.get`: absent means Google's default, `true`, and should fail as such.
    assert body.get("guestsCanSeeOtherGuests", True) is False
    assert body.get("guestsCanInviteOthers", True) is False


def test_a_missing_address_is_left_off_rather_than_sent_empty() -> None:
    """A party with no email on file is left off the guest list rather than sent
    as an empty address, which invites nobody and is not a valid guest."""
    api, seen = calendar(created())

    insert(api, wants_conference=False, guests=(MENTEE, ""))

    assert json.loads(seen[0].content)["attendees"] == [{"email": MENTEE}]


def test_the_calendar_id_is_configurable() -> None:
    """A secondary calendar keeps session events out of whatever else the
    platform account holds."""
    api, seen = calendar(created(hangoutLink="x"), calendar_id="sessions@group.calendar")

    insert(api, wants_conference=True)

    assert "sessions%40group.calendar" in str(seen[0].url) or "sessions@group.calendar" in str(
        seen[0].url
    )


# --------------------------------------------------------------------------
# Removing it
# --------------------------------------------------------------------------


def test_cancelling_deletes_the_event() -> None:
    """A called-off session must not leave a live meeting in either calendar."""
    api, seen = calendar(lambda _: httpx.Response(204))

    api.cancel_event("evt_1")

    assert seen[0].method == "DELETE"
    assert "evt_1" in str(seen[0].url)


def test_cancelling_tells_the_mentee_it_was_cancelled() -> None:
    """**The pair `create_event` forms with this, which was briefly broken.**

    The invitation passed `sendUpdates=all` and the cancellation passed nothing,
    so Google announced the booking and said nothing about it being called off:
    the event vanished from the mentee's calendar with no message. A duplicate
    cancellation is noise; a silent disappearance is somebody turning up to a
    session that is not happening.
    """
    api, seen = calendar(lambda _: httpx.Response(204))

    api.cancel_event("evt_1")

    assert "sendUpdates=all" in str(seen[0].url)


def test_both_ends_of_a_booking_notify_the_same_way() -> None:
    """Pins the pair, so neither can drift alone.

    Whichever way this project decides to go — Google announces both, or Google
    announces neither and the outbox carries it — the failure is one end
    changing without the other. That is what happened, and no test objected.
    """
    inviting, invited = calendar(created(hangoutLink="https://meet.google.com/abc"))
    insert(inviting, wants_conference=True)
    cancelling, cancelled_call = calendar(lambda _: httpx.Response(204))
    cancelling.cancel_event("evt_1")

    assert invited[0].url.params.get("sendUpdates") == cancelled_call[0].url.params.get(
        "sendUpdates"
    )


@pytest.mark.parametrize("status", [404, 410])
def test_an_event_already_gone_is_success(status: int) -> None:
    """**The state this method exists to reach.** Raising would make a retry
    fail forever on a session that is already correct, and would leave the
    stored id in place for a later run to try again with."""
    api, _ = calendar(lambda _: httpx.Response(status, json={"error": "gone"}))

    assert api.cancel_event("evt_1") is None


def test_a_real_failure_still_raises() -> None:
    """A 500 means the event may still be there, so the caller must not clear
    the id it needs to try again."""
    api, _ = calendar(lambda _: httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(VenueUnavailableError):
        api.cancel_event("evt_1")


# --------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------


def test_the_access_token_is_reused_across_calls() -> None:
    """A refresh per call is a second round trip to Google on every booking, to
    re-acquire something still valid for the best part of an hour."""
    exchanges = 0

    def route(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges
        if "oauth2" in str(request.url):
            exchanges += 1
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(200, json={"id": "evt", "hangoutLink": "x"})

    api = GoogleCalendar(
        client_id="cid",
        client_secret=FAKE_SECRET,
        refresh_token=FAKE_REFRESH,
        client=httpx.Client(transport=httpx.MockTransport(route)),
    )

    insert(api, wants_conference=True)
    insert(api, wants_conference=True)

    assert exchanges == 1


def test_a_refused_refresh_is_a_venue_failure_not_a_crash() -> None:
    """An expired or revoked refresh token is an operator problem, and the
    caller's answer is the same as for every other Google failure: carry on
    without a calendar rather than fail the booking."""
    api = GoogleCalendar(
        client_id="cid",
        client_secret=FAKE_SECRET,
        refresh_token=FAKE_REFRESH,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(400, json={"error": "invalid_grant"})
            )
        ),
    )

    with pytest.raises(VenueUnavailableError):
        insert(api, wants_conference=True)

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
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.infra.clients.meetings import (
    CONFERENCE_POLLS,
    CalendarAccessRevokedError,
    GoogleCalendar,
    VenueUnavailableError,
)

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
    join_url: str | None = SESSION_PAGE,
    guests: tuple[str, ...] = (MENTEE, MENTOR),
) -> Any:
    return api.create_event(
        organiser_id="sess-1",
        attendee_emails=guests,
        starts_at=STARTS,
        duration_minutes=60,
        summary="EduFurther session",
        join_url=join_url,
    )


# --------------------------------------------------------------------------
# Creating the event
# --------------------------------------------------------------------------


def test_no_event_asks_for_a_conference_when_it_is_created() -> None:
    """**The invite carries no Meet link, whatever the venue** (#384). The Meet
    is patched in at the last reminder, so the first way in is the session page,
    where pressing Join is recorded. A conference asked for here would put the
    link on the guests' calendars from the moment of booking."""
    api, seen = calendar(created())

    event = insert(api)

    (request,) = seen
    assert "conferenceDataVersion" not in request.url.params
    assert "conferenceData" not in json.loads(request.content)
    assert event is not None
    assert event.external_id == "evt_1"


def test_every_invite_links_to_the_session_page() -> None:
    """**The session page, for every venue (#389).** It is where Join is pressed
    and recorded, and the page redirects whatever later happens to the session,
    so it is safe on an invite that outlives a cancellation. A Daily room's own
    URL was written here before, and a bare room URL is *refused* by Daily
    without a token (`docs/daily-spike-guide.md` Q1): every Daily invite sent
    linked to a room that turned its guest away."""
    api, seen = calendar(created())

    insert(api)

    assert SESSION_PAGE in json.loads(seen[0].content).get("description", "")


def test_with_no_session_page_the_event_is_still_written_without_a_link() -> None:
    """No `APP_BASE_URL` means no link to give. The event still goes out, so both
    parties have the time in their calendars; a bare path or a venue URL would
    each be worse than nothing."""
    api, seen = calendar(created())

    event = insert(api, join_url=None)

    assert "description" not in json.loads(seen[0].content)
    assert event is not None


# --------------------------------------------------------------------------
# Adding the Meet later (#384)
# --------------------------------------------------------------------------

MEET = "https://meet.google.com/abc-defg-hij"


def conference(status: str, *, link: str | None = None) -> dict[str, Any]:
    """An event as Google returns it, its conference at `status`."""
    body: dict[str, Any] = {
        "id": "evt_1",
        "conferenceData": {"createRequest": {"status": {"statusCode": status}}},
    }
    if link:
        body["hangoutLink"] = link
    return body


#: An event as created: no conference on it yet.
NO_MEET: dict[str, Any] = {"id": "evt_1"}


def answers(*bodies: dict[str, Any]) -> Any:
    """Each call gets the next body; the last repeats."""
    queue = list(bodies)
    return lambda _: httpx.Response(200, json=queue.pop(0) if len(queue) > 1 else queue[0])


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.infra.clients.meetings.CONFERENCE_POLL_SECONDS", 0)


def test_the_meet_is_patched_in_silently_with_the_version_parameter() -> None:
    """**Three parameters, each a trap.** Without `conferenceDataVersion=1`
    Google accepts the write and drops the conference; without `sendUpdates=none`
    every guest gets a second email minutes before the session; and the request
    id is the caller's, so a repeat is the same Meet rather than a second."""
    api, seen = calendar(answers(NO_MEET, conference("success", link=MEET)))

    link = api.add_conference("evt_1", request_id="sess-1")

    read, request = seen
    assert read.method == "GET"
    assert read.url.params["conferenceDataVersion"] == "1"
    assert request.method == "PATCH"
    assert request.url.path.endswith("/events/evt_1")
    assert request.url.params["conferenceDataVersion"] == "1"
    assert request.url.params["sendUpdates"] == "none"
    create = json.loads(request.content)["conferenceData"]["createRequest"]
    assert create["requestId"] == "sess-1"
    assert create["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert link == MEET


def test_a_pending_conference_is_waited_for() -> None:
    """**Created asynchronously**, in Google's words: the patch can answer
    `pending` and turn `success` later, so the link is read again rather than
    taken as missing."""
    api, seen = calendar(
        answers(
            NO_MEET,
            conference("pending"),
            conference("pending"),
            conference("success", link=MEET),
        )
    )

    link = api.add_conference("evt_1", request_id="sess-1")

    assert link == MEET
    assert [request.method for request in seen] == ["GET", "PATCH", "GET", "GET"]
    assert seen[2].url.params["conferenceDataVersion"] == "1"


def test_an_event_that_has_its_meet_is_not_written_again() -> None:
    """**Measured on #402's spike:** a second patch a second after the first is
    refused, `403 Rate Limit Exceeded`. Two parties pressing Join together, or
    the reminder firing just after a press, would get no link for a Meet that
    exists. So the event is read first, and a link already there is the answer."""
    api, seen = calendar(answers(conference("success", link=MEET)))

    link = api.add_conference("evt_1", request_id="sess-1")

    assert link == MEET
    assert [request.method for request in seen] == ["GET"]


def test_a_conference_already_being_made_is_waited_for_not_requested_again() -> None:
    """Another caller's patch is in flight: wait for it rather than write again,
    which is what Google rate-limits."""
    api, seen = calendar(answers(conference("pending"), conference("success", link=MEET)))

    link = api.add_conference("evt_1", request_id="sess-1")

    assert link == MEET
    assert [request.method for request in seen] == ["GET", "GET"]


@pytest.mark.parametrize(
    "outcome",
    [conference("failure"), conference("pending"), conference("success")],
    ids=["refused", "never-ready", "success-without-a-link"],
)
def test_no_link_is_a_venue_failure(outcome: dict[str, Any]) -> None:
    """**A 200 with no link is a failure**, whichever way it happens: refused,
    still pending when the wait runs out, or `success` with the link missing.
    Returning nothing quietly would leave a session nobody can enter."""
    api, _ = calendar(answers(outcome))

    with pytest.raises(VenueUnavailableError):
        api.add_conference("evt_1", request_id="sess-1")


def test_the_wait_is_bounded() -> None:
    """It runs inside a request, so it gives up rather than holding the caller."""
    api, seen = calendar(answers(NO_MEET, conference("pending")))

    with pytest.raises(VenueUnavailableError):
        api.add_conference("evt_1", request_id="sess-1")

    assert len(seen) == 2 + CONFERENCE_POLLS


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
    api, seen = calendar(created())

    insert(api)

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
    api, seen = calendar(created())

    insert(api)

    body = json.loads(seen[0].content)
    # `.get`: absent means Google's default, `true`, and should fail as such.
    assert body.get("guestsCanSeeOtherGuests", True) is False
    assert body.get("guestsCanInviteOthers", True) is False


def test_a_missing_address_is_left_off_rather_than_sent_empty() -> None:
    """A party with no email on file is left off the guest list rather than sent
    as an empty address, which invites nobody and is not a valid guest."""
    api, seen = calendar(created())

    insert(api, guests=(MENTEE, ""))

    assert json.loads(seen[0].content)["attendees"] == [{"email": MENTEE}]


def test_the_calendar_id_is_configurable() -> None:
    """A secondary calendar keeps session events out of whatever else the
    platform account holds."""
    api, seen = calendar(created(), calendar_id="sessions@group.calendar")

    insert(api)

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
    inviting, invited = calendar(created())
    insert(inviting)
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

    insert(api)
    insert(api)

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
        insert(api)


class Google:
    """A stub Google that numbers the tokens it mints and answers calendar
    calls from a queue, defaulting to `204`."""

    def __init__(self, *calls: httpx.Response, grants: Sequence[httpx.Response] = ()) -> None:
        self.exchanges: list[dict[str, list[str]]] = []
        self.calls: list[httpx.Request] = []
        self._calls = list(calls)
        self._grants = list(grants)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            self.exchanges.append(parse_qs(request.content.decode()))
            time.sleep(0.01)
            if self._grants:
                return self._grants.pop(0)
            token = f"at-{len(self.exchanges)}"
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        self.calls.append(request)
        return self._calls.pop(0) if self._calls else httpx.Response(204)

    def bearers(self) -> list[str]:
        return [r.headers["Authorization"] for r in self.calls]


def platform(
    client: httpx.Client, *, refresh: str = FAKE_REFRESH, calendar_id: str = "primary"
) -> GoogleCalendar:
    """A fresh adapter, as `calendar_for` builds one per call."""
    return GoogleCalendar(
        client_id="cid",
        client_secret=FAKE_SECRET,
        refresh_token=refresh,
        calendar_id=calendar_id,
        client=client,
    )


def test_two_calendars_with_one_account_exchange_the_token_once() -> None:
    """#421: `calendar_for` builds an adapter per call, so a token cached on
    the instance was re-exchanged before every accept, cancel and runner tick."""
    google = Google()
    with httpx.Client(transport=httpx.MockTransport(google)) as client:
        platform(client).cancel_event("evt_1")
        platform(client).cancel_event("evt_2")

    assert len(google.exchanges) == 1
    assert google.bearers() == ["Bearer at-1", "Bearer at-1"]


def test_another_refresh_token_gets_its_own_token() -> None:
    """Keyed on the grant, so one account never borrows another's token."""
    google = Google()
    with httpx.Client(transport=httpx.MockTransport(google)) as client:
        platform(client).cancel_event("evt_1")
        platform(client, refresh="another-refresh-token").cancel_event("evt_2")

    assert [e["refresh_token"] for e in google.exchanges] == [
        [FAKE_REFRESH],
        ["another-refresh-token"],
    ]
    assert google.bearers() == ["Bearer at-1", "Bearer at-2"]


def test_another_calendar_on_one_account_shares_the_token() -> None:
    """The token belongs to the grant, not the calendar it is used on."""
    google = Google()
    with httpx.Client(transport=httpx.MockTransport(google)) as client:
        platform(client).cancel_event("evt_1")
        platform(client, calendar_id="team").cancel_event("evt_2")

    assert len(google.exchanges) == 1
    assert "/calendars/team/" in str(google.calls[1].url)


def test_threads_arriving_together_exchange_the_token_once() -> None:
    """Calendars are reached through `asyncio.to_thread`, so callers finding
    no token at the same moment must not each exchange one."""
    google = Google()
    arrived = threading.Barrier(8)

    with httpx.Client(transport=httpx.MockTransport(google)) as client:

        def cancel(n: int) -> None:
            arrived.wait()
            platform(client).cancel_event(f"evt_{n}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(cancel, range(8)))

    assert len(google.exchanges) == 1
    assert len(google.calls) == 8


def test_a_rejected_cached_token_is_exchanged_again_and_the_call_retried() -> None:
    """A token revoked or rotated before it expires answers `401`. Without a
    retry the cache would fail every call with it for up to an hour."""
    google = Google(httpx.Response(204), httpx.Response(401))
    with httpx.Client(transport=httpx.MockTransport(google)) as client:
        platform(client).cancel_event("evt_1")
        platform(client).cancel_event("evt_2")
        platform(client).cancel_event("evt_3")

    assert len(google.exchanges) == 2
    assert google.bearers() == ["Bearer at-1", "Bearer at-1", "Bearer at-2", "Bearer at-2"]


def test_a_second_401_is_a_venue_failure_after_one_retry() -> None:
    google = Google(httpx.Response(401), httpx.Response(401))
    with (
        httpx.Client(transport=httpx.MockTransport(google)) as client,
        pytest.raises(VenueUnavailableError, match="401"),
    ):
        platform(client).cancel_event("evt_1")

    assert len(google.exchanges) == 2
    assert len(google.calls) == 2


def test_a_grant_revoked_behind_a_cached_token_is_still_reported_as_revoked() -> None:
    """The `401` is what makes the dead grant visible again: the re-exchange
    hears `invalid_grant`, as an uncached call would have."""
    google = Google(
        httpx.Response(204),
        httpx.Response(401),
        grants=[
            httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600}),
            httpx.Response(400, json={"error": "invalid_grant"}),
        ],
    )
    with httpx.Client(transport=httpx.MockTransport(google)) as client:
        platform(client).cancel_event("evt_1")
        with pytest.raises(CalendarAccessRevokedError):
            platform(client).cancel_event("evt_2")

    assert len(google.exchanges) == 2
    assert google.bearers() == ["Bearer at-1", "Bearer at-1"]

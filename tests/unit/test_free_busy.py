"""Reading when a mentor is busy in their own calendar.

**These assert the request, like the consent tests beside them.** The failure
that matters here is silent in the response: asking the *wrong calendar* returns
a perfectly well-formed answer about somebody else's diary, and asking day by
day returns the right answer at fifty-six times the cost.

**And one failure is not a failure at all in the status code.** `freeBusy`
answers `200` with per-calendar trouble reported inside the body, so a revoked
grant arrives looking like a mentor with nothing on. Reading past that would
report a full diary as completely free — the exact inversion of what this exists
to prevent.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.domain.availability import UtcInterval
from app.infra.clients.meetings import (
    GOOGLE_TOKEN_URL,
    CalendarAccessRevokedError,
    VenueUnavailableError,
    access_token,
    free_busy,
)

START = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
END = dt.datetime(2026, 10, 27, tzinfo=dt.UTC)  # 56 days, the projection maximum.


def transport(*, token: httpx.Response, busy: httpx.Response) -> tuple[httpx.Client, list[str]]:
    """A client that answers the token exchange and then the free/busy read."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return token if str(request.url) == GOOGLE_TOKEN_URL else busy

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def ok_token() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})


def reading(payload: dict[str, object], **kwargs: object) -> tuple[object, list[str]]:
    client, seen = transport(token=ok_token(), busy=httpx.Response(200, json=payload))
    with client:
        result = free_busy(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="rt",  # noqa: S106
            start=kwargs.get("start", START),  # type: ignore[arg-type]
            end=kwargs.get("end", END),  # type: ignore[arg-type]
            client=client,
        )
    return result, seen


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


def test_it_reads_the_mentors_own_calendar_and_not_the_platforms() -> None:
    """`primary`, never `google_calendar_id`.

    That setting names a calendar on **EduFurther's** account. Pointing a
    mentor's free/busy read at it would subtract the platform's own diary from
    every mentor's availability — a wrong answer that looks entirely plausible.
    """
    body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_URL:
            return ok_token()
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"calendars": {"primary": {"busy": []}}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        free_busy(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="rt",  # noqa: S106
            start=START,
            end=END,
            client=client,
        )

    assert body["items"] == [{"id": "primary"}]
    assert body["timeMin"] == START.isoformat()
    assert body["timeMax"] == END.isoformat()


def test_a_fifty_six_day_span_is_one_request_not_fifty_six() -> None:
    """The projection maximum, on an endpoint that takes no token."""
    _, seen = reading({"calendars": {"primary": {"busy": []}}})

    # The token exchange, then the read. Not fifty-seven.
    assert len(seen) == 2
    assert seen.count(GOOGLE_TOKEN_URL) == 1


# --------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------


def test_busy_periods_come_back_as_utc_intervals() -> None:
    result, _ = reading(
        {
            "calendars": {
                "primary": {
                    "busy": [
                        {"start": "2026-09-02T09:00:00Z", "end": "2026-09-02T10:00:00Z"},
                        {"start": "2026-09-03T14:00:00+02:00", "end": "2026-09-03T15:00:00+02:00"},
                    ]
                }
            }
        }
    )

    assert result == (
        UtcInterval(
            dt.datetime(2026, 9, 2, 9, tzinfo=dt.UTC), dt.datetime(2026, 9, 2, 10, tzinfo=dt.UTC)
        ),
        UtcInterval(
            dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC), dt.datetime(2026, 9, 3, 13, tzinfo=dt.UTC)
        ),
    )
    # **The equality above proves nothing about the offset.** Aware datetimes
    # compare by instant, so `14:00+02:00 == 12:00Z` and dropping the conversion
    # passes it — a mutation demonstrated exactly that. The type is called
    # `UtcInterval`; holding a `+02:00` value in one is a lie regardless of how
    # it compares, so the tzinfo is what gets asserted.
    assert all(
        moment.tzinfo is dt.UTC for period in result for moment in (period.start, period.end)
    )


def test_a_free_calendar_is_an_empty_tuple() -> None:
    result, _ = reading({"calendars": {"primary": {"busy": []}}})

    assert result == ()


def test_a_per_calendar_error_is_a_failure_rather_than_a_free_mentor() -> None:
    """The 200 that would otherwise report a full diary as completely free."""
    with pytest.raises(VenueUnavailableError, match="refused the calendar"):
        reading({"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}})


def test_a_response_that_is_not_an_object_is_refused() -> None:
    client, _ = transport(token=ok_token(), busy=httpx.Response(200, json=["nope"]))
    with client, pytest.raises(VenueUnavailableError):
        free_busy(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="rt",  # noqa: S106
            start=START,
            end=END,
            client=client,
        )


def test_google_being_unreachable_raises_rather_than_returning_free() -> None:
    """Failing open is the *caller's* decision, not this function's.

    Returning `()` here would make an outage indistinguishable from a mentor
    with nothing booked, and the caller could no longer choose.
    """
    client, _ = transport(token=ok_token(), busy=httpx.Response(503))
    with client, pytest.raises(VenueUnavailableError):
        free_busy(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="rt",  # noqa: S106
            start=START,
            end=END,
            client=client,
        )


# --------------------------------------------------------------------------
# A dead grant is not a bad afternoon
# --------------------------------------------------------------------------


def test_invalid_grant_is_its_own_error() -> None:
    """The mentor revoked us. Retrying fails identically, forever."""
    client, _ = transport(
        token=httpx.Response(400, json={"error": "invalid_grant"}),
        busy=httpx.Response(200, json={}),
    )
    with client, pytest.raises(CalendarAccessRevokedError):
        access_token(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="stale",  # noqa: S106
            client=client,
        )


def test_any_other_refusal_stays_transient() -> None:
    """A 500 from the token endpoint must not cost a mentor their connection."""
    client, _ = transport(token=httpx.Response(500), busy=httpx.Response(200, json={}))
    with client, pytest.raises(VenueUnavailableError) as raised:
        access_token(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="rt",  # noqa: S106
            client=client,
        )

    assert not isinstance(raised.value, CalendarAccessRevokedError)


def test_a_working_exchange_returns_the_token_and_its_lifetime() -> None:
    client, _ = transport(token=ok_token(), busy=httpx.Response(200, json={}))
    with client:
        assert access_token(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="rt",  # noqa: S106
            client=client,
        ) == ("at", 3599)

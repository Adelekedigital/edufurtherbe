"""One place chooses each adapter, for the request path and the job runner alike."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.sessions import LIVE_SESSION_STATUSES
from app.infra.clients import meetings
from app.infra.clients.meetings import (
    DailyRooms,
    GoogleCalendar,
    NullCalendar,
    NullRooms,
    VenueUnavailableError,
    calendar_for,
    rooms_for,
)
from app.infra.clients.scheduler import NullScheduler, QStashScheduler, scheduler_for
from app.infra.db.models.sessions import LIVE_STATUSES

CALENDAR = {
    "google_oauth_client_id": "client",
    "google_oauth_client_secret": SecretStr("secret"),
    "google_calendar_refresh_token": SecretStr("refresh"),
}


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_rooms_are_daily_with_a_key_and_null_without() -> None:
    assert isinstance(rooms_for(settings(daily_api_key=SecretStr("k"))), DailyRooms)
    assert isinstance(rooms_for(settings()), NullRooms)


def test_the_calendar_is_google_with_all_three_values() -> None:
    assert isinstance(calendar_for(settings(**CALENDAR)), GoogleCalendar)


@pytest.mark.parametrize("missing", sorted(CALENDAR))
def test_the_calendar_is_null_when_any_one_value_is_missing(missing: str) -> None:
    """Two of three is a half-finished setup, not a working calendar."""
    assert isinstance(calendar_for(settings(**{**CALENDAR, missing: None})), NullCalendar)


def exchanges_counted(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every token exchange the factory's calendars make, against a stub."""
    exchanges: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            exchanges.append(request)
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(204)

    stub = httpx.Client(transport=httpx.MockTransport(answer))
    monkeypatch.setattr(meetings, "shared", lambda _timeout: stub)
    return exchanges


def test_two_calls_to_the_factory_exchange_the_refresh_token_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#421: a calendar per call cached its token per instance, so every
    accept, cancel and runner tick paid an OAuth round trip first."""
    exchanges = exchanges_counted(monkeypatch)

    calendar_for(settings(**CALENDAR)).cancel_event("evt_1")
    calendar_for(settings(**CALENDAR)).cancel_event("evt_2")

    assert len(exchanges) == 1


def test_other_credentials_get_their_own_calendar_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyed on the values, so another account never borrows this one's token."""
    exchanges = exchanges_counted(monkeypatch)
    other = {**CALENDAR, "google_calendar_refresh_token": SecretStr("another")}

    first = calendar_for(settings(**CALENDAR))
    second = calendar_for(settings(**other))
    first.cancel_event("evt_1")
    second.cancel_event("evt_2")

    assert first is not second
    assert first is calendar_for(settings(**CALENDAR))
    assert [parse_qs(r.content.decode())["refresh_token"] for r in exchanges] == [
        ["refresh"],
        ["another"],
    ]


def test_the_scheduler_is_qstash_with_a_token_in_its_region_and_null_without() -> None:
    region = "https://qstash-us-east-1.upstash.io"
    chosen = scheduler_for(settings(qstash_token=SecretStr("t"), qstash_url=region))

    assert isinstance(chosen, QStashScheduler)
    assert chosen._publish == f"{region}/v2/publish"
    assert isinstance(scheduler_for(settings()), NullScheduler)


def test_null_rooms_refuse_a_token_as_no_venue_rather_than_crashing() -> None:
    """A Daily session whose key was later removed: `/door` catches
    `VenueUnavailableError` and answers "no door"; an `AttributeError` was a 500."""
    now = dt.datetime.now(dt.UTC)

    with pytest.raises(VenueUnavailableError):
        NullRooms().token_for(
            room="r",
            user_id="u",
            user_name="n",
            is_owner=False,
            opens_at=now,
            closes_at=now,
        )


def test_the_sql_live_predicate_is_the_domain_set() -> None:
    """Built from `LIVE_SESSION_STATUSES`, so the queries and the extract's
    overlap check follow it. The constraint and indexes are migrations; the
    parity test in `test_models` fails until a new one restates the set."""
    quoted = re.fullmatch(r"status IN \((.*)\)", LIVE_STATUSES)

    assert quoted is not None
    assert quoted.group(1).split(", ") == [f"'{s.value}'" for s in LIVE_SESSION_STATUSES]
    assert [s.value for s in LIVE_SESSION_STATUSES] == ["pending_mentor_approval", "confirmed"]

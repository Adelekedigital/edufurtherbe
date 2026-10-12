"""One place chooses each adapter, for the request path and the job runner alike."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.sessions import LIVE_SESSION_STATUSES
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

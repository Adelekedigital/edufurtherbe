"""A job in production never runs on a null adapter (the settle-sessions
recovery script would otherwise mark real users' emails sent)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.infra.clients.meetings import DailyRooms, GoogleCalendar, NullCalendar, NullRooms
from app.infra.clients.notifications import LoopsNotifier, NullNotifier
from app.infra.jobs.runner import RuntimeJobs

LIVE = {
    "loops_api_key": SecretStr("loops"),
    "daily_api_key": SecretStr("daily"),
    "google_oauth_client_id": "cid",
    "google_oauth_client_secret": SecretStr("secret"),
    "google_calendar_refresh_token": SecretStr("refresh"),
}


def jobs(**values: object) -> RuntimeJobs:
    return RuntimeJobs(Settings(_env_file=None, **values))


@pytest.mark.parametrize(
    ("adapter", "named"),
    [
        ("_notifier", "LOOPS_API_KEY"),
        ("_calendar", "GOOGLE_OAUTH_CLIENT_ID"),
        ("_rooms", "DAILY_API_KEY"),
    ],
)
def test_production_refuses_a_null_adapter_and_names_the_setting(adapter: str, named: str) -> None:
    with pytest.raises(ConfigurationError, match=named):
        getattr(jobs(environment="production"), adapter)()


def test_production_with_the_providers_set_gets_the_real_adapters() -> None:
    runner = jobs(environment="production", **LIVE)

    assert isinstance(runner._notifier(), LoopsNotifier)
    assert isinstance(runner._calendar(), GoogleCalendar)
    assert isinstance(runner._rooms(), DailyRooms)


@pytest.mark.parametrize("environment", ["local", "ci", "staging"])
def test_elsewhere_the_null_adapters_still_stand_in(environment: str) -> None:
    runner = jobs(environment=environment)

    assert isinstance(runner._notifier(), NullNotifier)
    assert isinstance(runner._calendar(), NullCalendar)
    assert isinstance(runner._rooms(), NullRooms)

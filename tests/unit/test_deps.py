"""Dependency wiring.

``deps.py`` is a composition point exempt from the layer check, so nothing else
verifies it. Without this the module sat at 0% coverage — the wiring could stop
resolving and no test would notice.
"""

from types import SimpleNamespace
from typing import Any, cast, get_args

from fastapi import Request

from app.api.deps import SettingsDep, _calendar, _configured, _rooms
from app.core.config import Settings
from app.infra.clients.meetings import DailyRooms, GoogleCalendar, NullCalendar, NullRooms


def test_settings_dependency_resolves_to_settings() -> None:
    annotated_type, dependency = get_args(SettingsDep)

    assert annotated_type is Settings
    assert dependency.dependency is not None
    assert dependency.dependency() == Settings()


def fake_request(**state: object) -> Request:
    """A request carrying only what these dependencies read off `app.state`."""
    app = SimpleNamespace(state=SimpleNamespace(**state))
    return cast(Any, SimpleNamespace(app=app))


def test_configured_prefers_the_apps_own_settings() -> None:
    """The whole point: an app built with settings does not read the cache."""
    mine = Settings(_env_file=None, daily_api_key="from-the-app")

    assert _configured(fake_request(settings=mine)) is mine


def test_configured_falls_back_when_an_app_carries_none() -> None:
    assert _configured(fake_request()) == Settings()


def test_the_room_provider_reads_the_apps_settings() -> None:
    """A key on the app was ignored while this called `get_settings()`.

    Nothing caught it, because every test that exercises rooms wires a fake onto
    `app.state.meeting_rooms` and never reaches the branch that builds one.
    """
    configured = fake_request(settings=Settings(_env_file=None, daily_api_key="a-key"))

    assert isinstance(_rooms(configured), DailyRooms)
    assert isinstance(_rooms(fake_request(settings=Settings(_env_file=None))), NullRooms)


def test_the_calendar_reads_the_apps_settings() -> None:
    """All three or none — two of three is a half-finished setup."""
    whole = Settings(
        _env_file=None,
        google_oauth_client_id="cid",
        google_oauth_client_secret="secret",  # noqa: S106
        google_calendar_refresh_token="rt",  # noqa: S106
    )
    partial = Settings(_env_file=None, google_oauth_client_id="cid")

    assert isinstance(_calendar(fake_request(settings=whole)), GoogleCalendar)
    assert isinstance(_calendar(fake_request(settings=partial)), NullCalendar)


def test_a_wired_adapter_still_wins_over_configuration() -> None:
    """`app.state` is the composition root's override, not a fallback."""
    sentinel = object()
    request = fake_request(
        meeting_rooms=sentinel, settings=Settings(_env_file=None, daily_api_key="a-key")
    )

    assert _rooms(request) is sentinel

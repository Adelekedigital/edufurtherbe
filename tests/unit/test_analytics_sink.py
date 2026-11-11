"""PRD 30 PR 1: the event shape, the settings, the factory and the PostHog adapter.

The vendor is an `httpx.MockTransport`; nothing here reaches a network.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError
from tests.posthog_fakes import recording_client

from app.core.config import Settings, analytics_gaps, env_key
from app.core.errors import ConfigurationError, UpstreamError
from app.domain.analytics_events import (
    PERSONAL_KEYS,
    PERSONAL_WORDS,
    AnalyticsEvent,
    analytics_event,
)
from app.domain.notifications import Channel
from app.infra.clients import posthog
from app.infra.clients.posthog import (
    NullAnalyticsSink,
    PostHogSink,
    analytics_sink_for,
    live_analytics_sink,
)
from app.main import create_app

USER = UUID("0190aaaa-0000-7000-8000-000000000001")
SESSION = UUID("0190aaaa-0000-7000-8000-0000000000aa")
AT = dt.datetime(2026, 11, 20, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
HOST = "https://ingest.example.test"
TOKEN = "phc_project_token"  # noqa: S105 - a public ingestion token, made up


@pytest.fixture(autouse=True)
def no_process_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in Settings.model_fields:
        monkeypatch.delenv(env_key(name), raising=False)
    yield


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


ON = {"analytics_enabled": True, "analytics_host": HOST, "analytics_project_token": TOKEN}


# --------------------------------------------------------------------------
# The vocabulary and the event
# --------------------------------------------------------------------------


def test_the_server_sends_exactly_the_twelve_outcome_events() -> None:
    assert {event.value for event in AnalyticsEvent} == {
        "booking_requested",
        "session_confirmed",
        "session_completed",
        "session_cancelled",
        "session_no_show",
        "credit_spent",
        "credit_granted",
        "mentor_listed",
        "review_submitted",
        "referral_unlocked",
        "booking_received",
        "session_delivered",
    }
    assert Channel.ANALYTICS == "analytics"


def test_an_event_is_the_batch_shape_in_utc_with_the_user_id_as_identity() -> None:
    event = analytics_event(
        AnalyticsEvent.SESSION_CANCELLED,
        user_id=USER,
        at=AT,
        properties={"session_id": SESSION, "by": Channel.EMAIL, "count": 2, "late": False},
    )

    assert event == {
        "event": "session_cancelled",
        "distinct_id": str(USER),
        "timestamp": "2026-11-20T14:00:00+00:00",
        "properties": {
            "session_id": str(SESSION),
            "by": "email",
            "count": 2,
            "late": False,
            "$geoip_disable": True,
        },
    }
    assert json.loads(json.dumps(event)) == event


def test_an_event_without_properties_has_an_empty_set() -> None:
    event = analytics_event(AnalyticsEvent.MENTOR_LISTED, user_id=USER, at=AT)

    assert event["properties"] == {"$geoip_disable": True}


def test_every_event_skips_geoip_so_the_servers_address_is_not_read_as_the_users() -> None:
    """The server's IP would place every event in the data centre."""
    event = analytics_event(
        AnalyticsEvent.SESSION_COMPLETED, user_id=USER, at=AT, properties={"minutes": 45}
    )

    assert event["properties"]["$geoip_disable"] is True
    assert event["properties"]["minutes"] == 45


@pytest.mark.parametrize("key", [*sorted(PERSONAL_KEYS), *PERSONAL_WORDS, "Email", "FIRST_NAME"])
def test_a_key_naming_personal_data_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="personal data"):
        analytics_event(AnalyticsEvent.REVIEW_SUBMITTED, user_id=USER, at=AT, properties={key: 1})


@pytest.mark.parametrize(
    "value", ["ada@example.test", 4.5, {"nested": "x"}, ["a"], b"bytes", AT, object()]
)
def test_a_value_that_is_not_an_id_enum_count_or_code_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="analytics property 'rating'"):
        analytics_event(
            AnalyticsEvent.REVIEW_SUBMITTED, user_id=USER, at=AT, properties={"rating": value}
        )


@pytest.mark.parametrize(
    "key",
    [
        "mentor_name",
        "display_name",
        "Contact_Email",
        "phone_e164",
        "home_address",
        "bio_html",
        "free_text",
        "last_message",
        "username",
    ],
)
def test_a_key_containing_a_personal_word_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="personal data"):
        analytics_event(AnalyticsEvent.SESSION_CONFIRMED, user_id=USER, at=AT, properties={key: 1})


@pytest.mark.parametrize(
    "value", ["Ada Lovelace", "two words", "", "x" * 65, "a/b", "café", "line\nbreak"]
)
def test_a_string_that_is_not_an_id_enum_or_code_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="analytics property 'source'"):
        analytics_event(
            AnalyticsEvent.BOOKING_REQUESTED, user_id=USER, at=AT, properties={"source": value}
        )


def test_every_server_event_still_takes_ids_enums_counts_and_codes() -> None:
    """The positive case beside the guards: what the outcome events carry
    (ids, enum values, counts, flags, short codes) all leave."""
    properties: dict[str, object] = {
        "session_id": SESSION,
        "mentor_id": str(SESSION),
        "channel": Channel.WHATSAPP,
        "entry_point": "explore_card",
        "stage": "post-offer",
        "month": "2026-11",
        "version": "v1.2:beta",
        "credits": 3,
        "rating": 5,
        "late": True,
        "reason": None,
        "code": "x" * 64,
    }

    for event in AnalyticsEvent:
        sent = analytics_event(event, user_id=USER, at=AT, properties=properties)
        assert sent["properties"]["session_id"] == str(SESSION)
        assert sent["properties"]["channel"] == "whatsapp"
        assert sent["properties"]["code"] == "x" * 64


def test_a_time_without_a_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="zone"):
        analytics_event(AnalyticsEvent.CREDIT_SPENT, user_id=USER, at=AT.replace(tzinfo=None))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_analytics_is_off_and_unconfigured_by_default() -> None:
    found = settings()

    assert found.analytics_enabled is False
    assert found.analytics_host is None
    assert found.analytics_project_token is None
    assert found.analytics_api_key is None
    assert analytics_gaps(found) == []


def test_the_api_key_is_a_secret() -> None:
    found = settings(analytics_api_key="phx_private")

    assert isinstance(found.analytics_api_key, SecretStr)
    assert "phx_private" not in repr(found)


@pytest.mark.parametrize("value", ["", "  "])
def test_a_blank_host_or_token_is_unset(value: str) -> None:
    found = settings(analytics_host=value, analytics_project_token=value, analytics_api_key=value)

    assert (found.analytics_host, found.analytics_project_token, found.analytics_api_key) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "value", ["us.i.posthog.com", "ftp://us.i.posthog.com", "https://h.test/?a=1"]
)
def test_a_host_that_is_not_an_http_origin_is_refused(value: str) -> None:
    with pytest.raises(PydanticValidationError, match="ANALYTICS_HOST"):
        settings(analytics_host=value)


@pytest.mark.parametrize(
    ("missing", "named"),
    [
        ("analytics_host", ["ANALYTICS_HOST is not set"]),
        ("analytics_project_token", ["ANALYTICS_PROJECT_TOKEN is not set"]),
    ],
)
def test_switched_on_it_names_what_it_lacks_and_the_app_refuses_to_start(
    missing: str, named: list[str]
) -> None:
    found = settings(**(ON | {missing: None}))

    assert analytics_gaps(found) == named
    with pytest.raises(ConfigurationError, match=named[0]):
        create_app(found)


def test_switched_on_and_configured_the_app_starts() -> None:
    found = settings(**ON)

    assert analytics_gaps(found) == []
    assert create_app(found).state.settings is found


def test_configured_but_off_the_app_starts_and_sends_nothing() -> None:
    found = settings(**(ON | {"analytics_enabled": False}))

    assert analytics_gaps(found) == []
    assert isinstance(analytics_sink_for(found), NullAnalyticsSink)
    assert live_analytics_sink(found) is None


# --------------------------------------------------------------------------
# The factory
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"analytics_enabled": True},
        ON | {"analytics_host": None},
        ON | {"analytics_project_token": None},
    ],
)
def test_off_or_unconfigured_is_the_null_sink(values: dict[str, Any]) -> None:
    found = settings(**values)

    assert isinstance(analytics_sink_for(found), NullAnalyticsSink)
    assert live_analytics_sink(found) is None


def test_readiness_is_the_one_analytics_gaps_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory and the startup check cannot disagree: a gap the startup
    rule names is the null sink, whatever the settings look like."""
    monkeypatch.setattr(posthog, "analytics_gaps", lambda _settings: ["ANALYTICS_X is not set"])

    assert isinstance(analytics_sink_for(settings(**ON)), NullAnalyticsSink)


def test_on_and_configured_is_posthog() -> None:
    found = settings(**ON)

    assert isinstance(analytics_sink_for(found), PostHogSink)
    assert isinstance(live_analytics_sink(found), PostHogSink)


def test_the_null_sink_takes_everything_and_sends_nothing() -> None:
    sink = NullAnalyticsSink()

    assert (
        sink.capture([analytics_event(AnalyticsEvent.MENTOR_LISTED, user_id=USER, at=AT)]) is None
    )


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


EVENTS = [
    analytics_event(AnalyticsEvent.CREDIT_SPENT, user_id=USER, at=AT, properties={"amount": 1})
]


@pytest.mark.parametrize("historical", [False, True])
def test_capture_posts_one_batch_with_the_project_token(historical: bool) -> None:
    seen: list[httpx.Request] = []
    sink = PostHogSink(
        host=HOST + "/",
        project_token=TOKEN,
        historical_migration=historical,
        client=recording_client(seen),
    )

    sink.capture(EVENTS)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{HOST}/batch/"
    assert json.loads(request.content) == {
        "api_key": TOKEN,
        "batch": EVENTS,
        "historical_migration": historical,
    }


def test_nothing_to_capture_sends_nothing() -> None:
    seen: list[httpx.Request] = []

    PostHogSink(host=HOST, project_token=TOKEN, client=recording_client(seen)).capture([])

    assert seen == []


@pytest.mark.parametrize("status", [400, 401, 500, 503])
def test_a_refusal_is_an_upstream_error(status: int) -> None:
    sink = PostHogSink(host=HOST, project_token=TOKEN, client=recording_client(status=status))

    with pytest.raises(UpstreamError, match=str(status)):
        sink.capture(EVENTS)


def test_a_network_failure_is_an_upstream_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    sink = PostHogSink(
        host=HOST, project_token=TOKEN, client=httpx.Client(transport=httpx.MockTransport(down))
    )

    with pytest.raises(UpstreamError, match="refused"):
        sink.capture(EVENTS)

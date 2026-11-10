"""The pure rules the analytics backfill shares with the live events (PRD 30 PR 7)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from scripts.backfill_analytics import parser

from app.core.config import Settings
from app.domain.analytics_events import (
    HISTORY_NAMESPACE,
    AnalyticsEvent,
    first_confirmation,
    historical_event,
    history_event_id,
    session_event_names,
)
from app.domain.enums import SessionStatus
from app.infra.analytics_backfill import BackfillRefusedError, historical_sink

E = AnalyticsEvent
S = SessionStatus
AT = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.UTC)


def test_the_namespace_never_changes() -> None:
    """A new namespace would give every past fact a new uuid, and PostHog would
    keep both copies of anything sent twice."""
    assert UUID("401bd1b7-b938-49ad-ae51-48cb1b06581f") == HISTORY_NAMESPACE


def test_an_events_id_is_the_same_on_every_run_and_differs_by_event_and_fact() -> None:
    fact = uuid4()
    first = history_event_id(E.BOOKING_REQUESTED, fact)
    assert first == history_event_id(E.BOOKING_REQUESTED, fact)
    assert first != history_event_id(E.SESSION_CONFIRMED, fact)
    assert first != history_event_id(E.BOOKING_REQUESTED, uuid4())


def test_a_historical_event_is_the_live_shape_with_its_id() -> None:
    fact, user = uuid4(), uuid4()
    event = historical_event(
        E.SESSION_COMPLETED, fact_id=fact, user_id=user, at=AT, properties={"session_id": fact}
    )
    assert event == {
        "event": "session_completed",
        "distinct_id": str(user),
        "timestamp": AT.isoformat(),
        "properties": {"session_id": str(fact), "$geoip_disable": True},
        "uuid": str(history_event_id(E.SESSION_COMPLETED, fact)),
    }


def test_a_historical_event_keeps_the_live_guard() -> None:
    with pytest.raises(ValueError, match="personal"):
        historical_event(
            E.SESSION_COMPLETED, fact_id=uuid4(), user_id=uuid4(), at=AT, properties={"name": "x"}
        )


@pytest.mark.parametrize(
    ("moved_from", "first"),
    [
        (None, True),
        (S.PENDING_MENTOR_APPROVAL, True),
        (S.CONFIRMED, False),
    ],
)
def test_first_confirmation(moved_from: SessionStatus | None, first: bool) -> None:
    assert first_confirmation(moved_from) is first


@pytest.mark.parametrize(
    ("from_status", "to_status", "moved_from", "moved_on", "names"),
    [
        # A new booking: a request, and a confirmation when confirmed outright.
        (None, S.PENDING_MENTOR_APPROVAL, None, False, [E.BOOKING_REQUESTED]),
        (None, S.CONFIRMED, None, False, [E.BOOKING_REQUESTED, E.SESSION_CONFIRMED]),
        # A move is never a request; confirmed only when it was still waiting.
        (None, S.CONFIRMED, S.CONFIRMED, False, []),
        (None, S.CONFIRMED, S.PENDING_MENTOR_APPROVAL, False, [E.SESSION_CONFIRMED]),
        (None, S.PENDING_MENTOR_APPROVAL, S.CONFIRMED, False, []),
        # A request accepted.
        (S.PENDING_MENTOR_APPROVAL, S.CONFIRMED, None, False, [E.SESSION_CONFIRMED]),
        # Cancelled, unless it was a move's ending.
        (S.CONFIRMED, S.CANCELLED, None, False, [E.SESSION_CANCELLED]),
        (S.CONFIRMED, S.CANCELLED, None, True, []),
        # Settled.
        (S.CONFIRMED, S.COMPLETED, None, False, [E.SESSION_COMPLETED]),
        (S.CONFIRMED, S.NO_SHOW, None, False, [E.SESSION_NO_SHOW]),
        # Nothing the live code reports.
        (S.PENDING_MENTOR_APPROVAL, S.DECLINED, None, False, []),
        (S.PENDING_MENTOR_APPROVAL, S.WITHDRAWN, None, True, []),
        (S.PENDING_MENTOR_APPROVAL, S.EXPIRED, None, False, []),
    ],
)
def test_session_event_names(
    from_status: SessionStatus | None,
    to_status: SessionStatus,
    moved_from: SessionStatus | None,
    moved_on: bool,
    names: list[AnalyticsEvent],
) -> None:
    assert (
        session_event_names(from_status, to_status, moved_from=moved_from, moved_on=moved_on)
        == names
    )


# --------------------------------------------------------------------------
# The sink
# --------------------------------------------------------------------------


ON = {
    "analytics_enabled": True,
    "analytics_host": "https://eu.i.posthog.com",
    "analytics_project_token": "phc_test",
}


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "gap"),
    [
        ({"analytics_enabled": False}, "ANALYTICS_ENABLED"),
        ({"analytics_project_token": None}, "ANALYTICS_PROJECT_TOKEN"),
        ({"analytics_host": None}, "ANALYTICS_HOST"),
    ],
)
def test_sending_refuses_without_analytics_configured(overrides: dict[str, Any], gap: str) -> None:
    with pytest.raises(BackfillRefusedError, match=gap):
        historical_sink(settings(**{**ON, **overrides}))


def test_sending_marks_every_batch_a_historical_migration() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "Ok"})

    sink = historical_sink(
        settings(**ON), client=httpx.Client(transport=httpx.MockTransport(answer))
    )
    sink.capture([{"event": "mentor_listed"}])

    (request,) = seen
    assert json.loads(request.content)["historical_migration"] is True


def test_the_script_refuses_to_run_without_until(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        parser().parse_args(["--send"])
    assert exited.value.code == 2
    assert "--until" in capsys.readouterr().err


def test_the_script_takes_a_zoned_until() -> None:
    args = parser().parse_args(["--until", "2026-11-10T09:30:00+00:00"])
    assert args.until == dt.datetime(2026, 11, 10, 9, 30, tzinfo=dt.UTC)
    assert args.send is False

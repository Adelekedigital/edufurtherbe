"""The pure rules the analytics backfill shares with the live events (PRD 30 PR 7)."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from scripts import backfill_analytics as script
from scripts.backfill_analytics import parser, preamble

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.domain.analytics_events import (
    HISTORY_NAMESPACE,
    MENTOR_COUNTERPARTS,
    AnalyticsEvent,
    first_confirmation,
    historical_event,
    history_event_id,
    requested_properties,
    session_event_names,
    session_properties,
)
from app.domain.enums import SessionStatus
from app.infra.analytics_backfill import (
    BackfillRefusedError,
    _session_events,
    historical_sink,
    read_cursor,
    target_fingerprint,
)

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
# The mentor's side (decision 337)
# --------------------------------------------------------------------------


def test_a_request_and_a_completion_have_a_mentor_side_and_nothing_else_does() -> None:
    assert {
        E.BOOKING_REQUESTED: E.BOOKING_RECEIVED,
        E.SESSION_COMPLETED: E.SESSION_DELIVERED,
    } == MENTOR_COUNTERPARTS
    assert E.BOOKING_RECEIVED.value == "booking_received"
    assert E.SESSION_DELIVERED.value == "session_delivered"


def test_a_row_names_only_the_mentees_events() -> None:
    """The mentor's side is added by the writers, never named by a row."""
    for from_status, to_status in [(None, S.CONFIRMED), (S.CONFIRMED, S.COMPLETED)]:
        names = session_event_names(from_status, to_status, moved_from=None, moved_on=False)
        assert not set(names) & set(MENTOR_COUNTERPARTS.values())


@pytest.mark.parametrize(
    ("event", "properties"),
    [
        (E.BOOKING_RECEIVED, requested_properties(uuid4(), uuid4(), "mentor_profile")),
        (E.SESSION_DELIVERED, session_properties(uuid4(), None)),
    ],
)
def test_the_guard_accepts_the_mentor_sides_properties(
    event: AnalyticsEvent, properties: dict[str, object]
) -> None:
    built = historical_event(event, fact_id=uuid4(), user_id=uuid4(), at=AT, properties=properties)
    assert built["event"] == event.value
    assert built["properties"]["session_id"] == str(properties["session_id"])


def a_fact(
    from_status: SessionStatus | None, to_status: SessionStatus, **values: object
) -> dict[str, Any]:
    """One `session_event_facts` row, both people counted unless told."""
    return {
        "id": uuid4(),
        "at": AT,
        "user_id": uuid4(),
        "mentor_id": uuid4(),
        "mentee_counted": True,
        "mentor_counted": True,
        "session_id": uuid4(),
        "session_type_id": uuid4(),
        "entry_point": None,
        "from_status": from_status,
        "to_status": to_status,
        "reason_code": None,
        "by": None,
        "moved_from": None,
        "moved_on": False,
        "confirmed_at": None,
    } | values


def test_a_request_and_a_completion_carry_the_mentors_side() -> None:
    made = a_fact(None, S.PENDING_MENTOR_APPROVAL)
    held = a_fact(S.CONFIRMED, S.COMPLETED)

    requested, received = _session_events(made)
    completed, delivered = _session_events(held)

    assert (requested["event"], received["event"]) == ("booking_requested", "booking_received")
    assert (completed["event"], delivered["event"]) == ("session_completed", "session_delivered")
    for mentee_side, mentor_side, fact in [
        (requested, received, made),
        (completed, delivered, held),
    ]:
        assert mentee_side["distinct_id"] == str(fact["user_id"])
        assert mentor_side["distinct_id"] == str(fact["mentor_id"])
        assert mentor_side["properties"] == mentee_side["properties"]
        assert mentor_side["timestamp"] == mentee_side["timestamp"]
        assert mentor_side["uuid"] != mentee_side["uuid"]


def test_the_mentors_side_has_the_same_uuid_on_every_run() -> None:
    held = a_fact(S.CONFIRMED, S.COMPLETED)
    first = _session_events(held)
    assert first == _session_events(held)
    assert first[1]["uuid"] == str(history_event_id(E.SESSION_DELIVERED, held["id"]))


@pytest.mark.parametrize(
    ("mentee_counted", "mentor_counted", "names"),
    [
        (True, False, ["session_completed"]),
        (False, True, ["session_delivered"]),
        (False, False, []),
    ],
)
def test_each_side_is_sent_only_when_its_person_is_counted(
    mentee_counted: bool, mentor_counted: bool, names: list[str]
) -> None:
    held = a_fact(
        S.CONFIRMED, S.COMPLETED, mentee_counted=mentee_counted, mentor_counted=mentor_counted
    )
    assert [e["event"] for e in _session_events(held)] == names


def test_a_no_show_or_a_confirmation_has_no_mentor_side() -> None:
    for fact in [a_fact(S.CONFIRMED, S.NO_SHOW), a_fact(S.PENDING_MENTOR_APPROVAL, S.CONFIRMED)]:
        assert [e["distinct_id"] for e in _session_events(fact)] == [str(fact["user_id"])]


def test_a_legacy_confirmation_is_the_mentees_only() -> None:
    legacy = a_fact(S.CONFIRMED, S.COMPLETED, confirmed_at=AT, mentee_counted=False)
    assert [e["event"] for e in _session_events(legacy)] == ["session_delivered"]


# --------------------------------------------------------------------------
# The sink
# --------------------------------------------------------------------------


ON = {
    "analytics_enabled": True,
    "analytics_host": "https://eu.i.posthog.com",
    "analytics_project_token": "phc_test",
}


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


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


# --------------------------------------------------------------------------
# The cursor and the script
# --------------------------------------------------------------------------

UNTIL = dt.datetime(2026, 11, 10, 9, 30, tzinfo=dt.UTC)


def a_cursor(path: Path, **values: object) -> Path:
    path.write_text(
        json.dumps(
            {
                "kind": "reviews",
                "at": AT.isoformat(),
                "id": str(uuid4()),
                "until": UNTIL.isoformat(),
                "target": "a" * 64,
                **values,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_target_is_the_hash_of_host_and_token_never_the_token() -> None:
    target = target_fingerprint(settings(**ON))
    assert target == hashlib.sha256(b"https://eu.i.posthog.com\nphc_test").hexdigest()
    assert target != target_fingerprint(settings(**{**ON, "analytics_project_token": "phc_other"}))
    assert target != target_fingerprint(settings(**{**ON, "analytics_host": "https://x.example"}))
    assert target_fingerprint(settings(**{**ON, "analytics_host": None})) is None


def test_a_good_cursor_is_read(tmp_path: Path) -> None:
    path = a_cursor(tmp_path / "c.json")
    saved = json.loads(path.read_text())

    resume = read_cursor(path, until=UNTIL, target="a" * 64)

    assert resume is not None
    assert (resume.kind, resume.at, str(resume.id)) == ("reviews", AT, saved["id"])
    assert read_cursor(tmp_path / "none.json", until=UNTIL, target=None) is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"kind": "reviews", "at": AT.isoformat()}),
        json.dumps({"kind": "reviews", "at": "yesterday", "id": "x", "until": "y", "target": "a"}),
    ],
)
def test_a_corrupt_or_incomplete_cursor_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BackfillRefusedError, match="cursor"):
        read_cursor(path, until=UNTIL, target="a" * 64)


def test_a_cursor_for_another_until_or_target_is_refused(tmp_path: Path) -> None:
    path = a_cursor(tmp_path / "c.json")
    with pytest.raises(BackfillRefusedError, match="--until"):
        read_cursor(path, until=UNTIL + dt.timedelta(seconds=1), target="a" * 64)
    with pytest.raises(BackfillRefusedError, match="PostHog"):
        read_cursor(path, until=UNTIL, target="b" * 64)


def test_sending_announces_its_host_and_cutoff_and_never_the_token(tmp_path: Path) -> None:
    args = parser().parse_args(
        ["--until", UNTIL.isoformat(), "--send", "--cursor", str(tmp_path / "c.json")]
    )
    lines = preamble(args, settings(**ON))
    assert lines == ["host: https://eu.i.posthog.com", f"cutoff: {UNTIL.isoformat()}"]
    assert not any("phc_test" in line for line in lines)


@pytest.mark.parametrize("send", [True, False])
def test_a_run_from_a_cursor_says_where_it_resumes(tmp_path: Path, send: bool) -> None:
    cursor = a_cursor(tmp_path / "c.json", target=target_fingerprint(settings(**ON)))
    argv = ["--until", UNTIL.isoformat(), "--cursor", str(cursor)] + (["--send"] if send else [])

    lines = preamble(parser().parse_args(argv), settings(**ON))

    assert lines[-1] == f"resuming from reviews {AT.isoformat()}"


@dataclass
class _Engine:
    async def dispose(self) -> None:
        return None


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (UpstreamError("analytics capture failed: 503"), 3),
        (BackfillRefusedError("the cursor c.json is unreadable"), 1),
        (OSError("disk full"), 3),
    ],
)
def test_a_failed_run_exits_non_zero_with_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    raised: Exception,
    code: int,
) -> None:
    async def fails(*_: object, **__: object) -> None:
        raise raised

    monkeypatch.setattr(script, "get_settings", lambda: settings(**ON))
    monkeypatch.setattr(script, "create_database_engine", lambda _: _Engine())
    monkeypatch.setattr(script, "backfill_analytics", fails)
    args = parser().parse_args(
        ["--until", UNTIL.isoformat(), "--send", "--cursor", str(tmp_path / "c.json")]
    )

    assert asyncio.run(script.run(args)) == code

    out = capsys.readouterr().out.splitlines()
    assert out[-1].endswith(str(raised))
    assert "Traceback" not in "\n".join(out)
    assert not any("phc_test" in line for line in out)

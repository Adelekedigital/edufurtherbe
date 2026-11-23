"""`--apply` refuses unless every adapter a confirmation uses is configured
(decision 366): a missing one would confirm a session without its room, invite
or reminders and drop it from every later run."""

from __future__ import annotations

from typing import Any

import pytest
from scripts import confirm_migrated_sessions as cli

from app.infra.clients.meetings import NullCalendar, NullRooms
from app.infra.clients.scheduler import NullScheduler
from app.infra.db.session_writer import FollowUps


class Real:
    """Stands for a configured adapter: anything that is not a Null one."""


def wired(**changes: Any) -> FollowUps:
    values: dict[str, Any] = {
        "rooms": Real(),
        "calendar": Real(),
        "scheduler": Real(),
        "callback_url": "https://api.example.test/api/v1/callbacks/reminders",
        "app_base_url": "https://app.example.test",
    } | changes
    return FollowUps(**values)


def test_nothing_is_missing_when_all_is_configured() -> None:
    assert cli.missing(wired()) == []


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"rooms": NullRooms()}, "DAILY_API_KEY"),
        ({"calendar": NullCalendar()}, "GOOGLE_CALENDAR_REFRESH_TOKEN"),
        ({"scheduler": NullScheduler()}, "QSTASH_TOKEN"),
        ({"scheduler": None}, "QSTASH_TOKEN"),
        ({"callback_url": None}, "PUBLIC_BASE_URL"),
        ({"app_base_url": ""}, "APP_BASE_URL"),
    ],
)
def test_each_gap_is_named(change: dict[str, Any], named: str) -> None:
    assert any(named in gap for gap in cli.missing(wired(**change)))


def test_apply_with_a_gap_refuses_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: object())
    monkeypatch.setattr(cli, "follow_ups_for", lambda _: wired(rooms=NullRooms()))
    monkeypatch.setattr("sys.argv", ["confirm_migrated_sessions.py", "--apply"])

    def no_database(**_: Any) -> None:
        raise AssertionError("the database was reached")

    monkeypatch.setattr(cli, "_main", no_database)

    assert cli.main() == 1
    assert "refusing: not configured: DAILY_API_KEY" in capsys.readouterr().out

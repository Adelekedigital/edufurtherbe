"""`--apply` refuses unless drafting can actually run: a missing setting would
queue drafts the callback then skips (owner's backfill, 2026-11-25)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from scripts import queue_bio_drafts as cli

from app.core.config import Settings

READY = {
    "mentee_bio_draft_enabled": True,
    "qstash_token": SecretStr("token"),
    "public_base_url": "https://api.example.test",
}


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def assistant_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured router, without a real key: only `available` is read."""
    monkeypatch.setattr(cli, "writing_assistant_for", lambda _: SimpleNamespace(available=True))


@pytest.mark.usefixtures("assistant_on")
def test_nothing_is_missing_when_all_is_configured() -> None:
    assert cli.missing(settings(**READY)) == []


@pytest.mark.usefixtures("assistant_on")
@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"mentee_bio_draft_enabled": False}, "MENTEE_BIO_DRAFT_ENABLED"),
        ({"qstash_token": None}, "QSTASH_TOKEN"),
        ({"public_base_url": None}, "PUBLIC_BASE_URL"),
    ],
)
def test_each_gap_is_named(change: dict[str, Any], named: str) -> None:
    assert any(named in gap for gap in cli.missing(settings(**(READY | change))))


def test_an_unconfigured_assistant_is_named() -> None:
    gaps = cli.missing(settings(**READY))

    assert any("AI_ROUTER_URL" in gap for gap in gaps)


def test_apply_with_a_gap_refuses_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings())
    monkeypatch.setattr("sys.argv", ["queue_bio_drafts.py", "--apply"])

    def no_database(**_: Any) -> None:
        raise AssertionError("the database was reached")

    monkeypatch.setattr(cli, "_main", no_database)

    assert cli.main() == 1
    assert "refusing: not configured: MENTEE_BIO_DRAFT_ENABLED" in capsys.readouterr().out

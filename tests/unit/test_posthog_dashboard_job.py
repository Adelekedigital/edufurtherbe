"""The daily `sync-posthog-dashboards` job (decision 344): the script's sync,
run by QStash, so the internal ids in every insight never go stale."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.posthog_fakes import HOST, KEY, FakePostHog

from app.core.config import Settings
from app.infra.jobs import runner
from app.infra.posthog_dashboards import run as shared
from app.infra.posthog_dashboards.definitions import DASHBOARDS

JOB = "sync-posthog-dashboards"
INSIGHTS = sum(len(d.insights) for d in DASHBOARDS)
API = {"analytics_api_host": HOST, "analytics_project_id": "42", "analytics_api_key": KEY}


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def internal(monkeypatch: pytest.MonkeyPatch) -> tuple[list[UUID], list[Settings]]:
    """The ids the job reads, and the settings it read them with."""
    ids, read_with = [uuid4(), uuid4(), uuid4()], []

    async def read(given: Settings) -> list[UUID]:
        read_with.append(given)
        return ids

    monkeypatch.setattr(shared, "read_internal", read)
    return ids, read_with


def _over(monkeypatch: pytest.MonkeyPatch, posthog: FakePostHog) -> None:
    monkeypatch.setattr(runner, "posthog_admin_for", lambda _settings: posthog.admin())


@pytest.mark.asyncio
async def test_without_the_private_api_the_job_is_skipped_and_succeeds(
    internal: tuple[list[UUID], list[Settings]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING")
    for missing in API:
        partial = {k: v for k, v in API.items() if k != missing}

        result = await runner.RuntimeJobs(settings(**partial)).run(JOB)

        assert result.status == "no-op"
        assert result.counts == {"created": 0, "rewritten": 0, "deleted": 0, "internal": 0}
    assert internal[1] == []
    assert "PostHog dashboard sync skipped" in caplog.text


@pytest.mark.asyncio
async def test_a_run_applies_the_sync_and_reports_its_counts(
    monkeypatch: pytest.MonkeyPatch, internal: tuple[list[UUID], list[Settings]]
) -> None:
    posthog = FakePostHog()
    _over(monkeypatch, posthog)
    configured = settings(**API)

    first = await runner.RuntimeJobs(configured).run(JOB)

    assert first.status == "completed"
    assert first.counts == {"created": 5 + INSIGHTS, "rewritten": 0, "deleted": 0, "internal": 3}
    assert len(posthog.rows["insights"]) == INSIGHTS
    for insight in posthog.rows["insights"]:
        exclusion = insight["query"]["source"]["properties"][0]["key"]
        assert all(str(user) in exclusion for user in internal[0])
    # The job's own settings, never the process cache.
    assert internal[1] == [configured]

    second = await runner.RuntimeJobs(configured).run(JOB)

    assert second.counts == {"created": 0, "rewritten": 5 + INSIGHTS, "deleted": 0, "internal": 3}


@pytest.mark.asyncio
@pytest.mark.usefixtures("internal")
async def test_a_dry_run_plans_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    posthog = FakePostHog()
    _over(monkeypatch, posthog)

    result = await runner.RuntimeJobs(settings(**API)).run(JOB, dry_run=True)

    assert result.counts == {"created": 5 + INSIGHTS, "rewritten": 0, "deleted": 0, "internal": 3}
    assert posthog.writes == []
    assert posthog.rows == {"insights": [], "dashboards": []}


@pytest.mark.asyncio
@pytest.mark.usefixtures("internal")
async def test_the_sync_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outbound HTTP never holds the loop (#370): QStash delivers the job as a
    request to this same app."""
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def fake_sync(*_: Any, **__: Any) -> Any:
        seen.append(threading.get_ident())
        return shared.Plan()

    monkeypatch.setattr(shared, "sync", fake_sync)
    _over(monkeypatch, FakePostHog())

    await runner.RuntimeJobs(settings(**API)).run(JOB)

    assert seen and seen[0] != loop_thread

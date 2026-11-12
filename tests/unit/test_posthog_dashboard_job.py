"""The daily `sync-posthog-dashboards` job (decision 344): the script's sync,
run by QStash, so the internal ids in every insight never go stale."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.posthog_fakes import HOST, KEY, FakePostHog, FakeSession, stub_reads

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.infra.jobs import runner
from app.infra.posthog_dashboards import run as sync_run
from app.infra.posthog_dashboards.definitions import DASHBOARDS

JOB = "sync-posthog-dashboards"
INSIGHTS = sum(len(d.insights) for d in DASHBOARDS)
ITEMS = 5 + INSIGHTS
API = {"analytics_api_host": HOST, "analytics_project_id": "42", "analytics_api_key": KEY}
NOTHING = {"created": 0, "rewritten": 0, "deleted": 0, "unchanged": 0, "internal": 0}


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class Engines:
    """The runner's engine, stubbed: what was built from which settings, and
    whether each was disposed."""

    def __init__(self) -> None:
        self.built: list[Settings] = []
        self.disposed = 0

    def __call__(self, given: Settings) -> Engines:
        self.built.append(given)
        return self

    async def dispose(self) -> None:
        self.disposed += 1


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch) -> Engines:
    made = Engines()
    monkeypatch.setattr(runner, "create_database_engine", made)
    monkeypatch.setattr(runner, "create_session_factory", lambda _engine: FakeSession)
    return made


@pytest.fixture
def internal() -> list[UUID]:
    return [uuid4(), uuid4(), uuid4()]


def _over(monkeypatch: pytest.MonkeyPatch, posthog: FakePostHog) -> None:
    monkeypatch.setattr(runner, "posthog_admin_for", lambda _settings: posthog.admin())


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "staging"])
async def test_without_the_private_api_the_job_is_skipped_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    engines: Engines,
    caplog: pytest.LogCaptureFixture,
    environment: str,
) -> None:
    read = stub_reads(monkeypatch, [uuid4()])
    caplog.set_level("WARNING")
    for missing in API:
        partial = {k: v for k, v in API.items() if k != missing}
        unset = settings(**partial).model_copy(update={"environment": environment})

        result = await runner.RuntimeJobs(unset).run(JOB)

        assert result.status == "no-op"
        assert result.counts == NOTHING
    assert read == []
    assert engines.built == []
    assert "PostHog dashboard sync skipped" in caplog.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("engines")
async def test_in_production_missing_settings_fail_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once production turns the job on, a missing setting is an error QStash
    and the alerts see, never a quiet no-op (decision 344)."""
    read = stub_reads(monkeypatch, [uuid4()])
    for missing in API:
        partial = {k: v for k, v in API.items() if k != missing}
        production = settings(**partial).model_copy(update={"environment": "production"})

        with pytest.raises(ConfigurationError, match="ANALYTICS_API_KEY"):
            await runner.RuntimeJobs(production).run(JOB)
    assert read == []
    # Configured, production runs it.
    posthog = FakePostHog()
    _over(monkeypatch, posthog)
    configured = settings(**API).model_copy(update={"environment": "production"})
    result = await runner.RuntimeJobs(configured).run(JOB)
    assert result.counts["created"] == ITEMS


@pytest.mark.asyncio
async def test_a_run_applies_the_sync_and_a_second_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, engines: Engines, internal: list[UUID]
) -> None:
    read = stub_reads(monkeypatch, internal)
    posthog = FakePostHog()
    _over(monkeypatch, posthog)
    configured = settings(**API)

    first = await runner.RuntimeJobs(configured).run(JOB)

    assert first.status == "completed"
    assert first.counts == NOTHING | {"created": ITEMS, "internal": 3}
    assert len(posthog.rows["insights"]) == INSIGHTS
    for insight in posthog.rows["insights"]:
        exclusion = insight["query"]["source"]["properties"][0]["key"]
        assert all(str(user) in exclusion for user in internal)
    # Through the job's own engine, built from its own settings and disposed.
    assert len(read) == 1
    assert engines.built == [configured]
    assert engines.disposed == 1

    second = await runner.RuntimeJobs(configured).run(JOB)

    # Unchanged and internal describe the run; they are not work done.
    assert second.status == "no-op"
    assert second.counts == NOTHING | {"unchanged": ITEMS, "internal": 3}


@pytest.mark.asyncio
@pytest.mark.usefixtures("engines")
async def test_a_new_internal_account_rewrites_the_insights(
    monkeypatch: pytest.MonkeyPatch, internal: list[UUID]
) -> None:
    posthog = FakePostHog()
    _over(monkeypatch, posthog)
    stub_reads(monkeypatch, internal)
    await runner.RuntimeJobs(settings(**API)).run(JOB)
    stub_reads(monkeypatch, [*internal, uuid4()])

    result = await runner.RuntimeJobs(settings(**API)).run(JOB)

    assert result.status == "completed"
    assert result.counts == NOTHING | {"rewritten": INSIGHTS, "unchanged": 5, "internal": 4}


@pytest.mark.asyncio
@pytest.mark.usefixtures("engines")
async def test_a_dry_run_plans_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, internal: list[UUID]
) -> None:
    stub_reads(monkeypatch, internal)
    posthog = FakePostHog()
    _over(monkeypatch, posthog)

    result = await runner.RuntimeJobs(settings(**API)).run(JOB, dry_run=True)

    assert result.counts == NOTHING | {"created": ITEMS, "internal": 3}
    assert posthog.writes == []
    assert posthog.rows == {"insights": [], "dashboards": []}


@pytest.mark.asyncio
async def test_a_run_that_finds_another_running_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
    engines: Engines,
    internal: list[UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    read = stub_reads(monkeypatch, internal, free=False)
    posthog = FakePostHog()
    _over(monkeypatch, posthog)
    caplog.set_level("INFO")

    result = await runner.RuntimeJobs(settings(**API)).run(JOB)

    assert result.status == "no-op"
    assert result.counts == NOTHING
    assert posthog.writes == []
    assert read == []
    assert "another PostHog dashboard sync is running" in caplog.text
    assert engines.disposed == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("engines")
async def test_the_sync_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, internal: list[UUID]
) -> None:
    """Outbound HTTP never holds the loop (#370): QStash delivers the job as a
    request to this same app."""
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def fake_sync(*_: Any, **__: Any) -> Any:
        seen.append(threading.get_ident())
        return sync_run.Plan()

    stub_reads(monkeypatch, internal)
    monkeypatch.setattr(sync_run, "sync", fake_sync)
    _over(monkeypatch, FakePostHog())

    await runner.RuntimeJobs(settings(**API)).run(JOB)

    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_a_cancelled_run_holds_on_until_its_thread_ends(
    monkeypatch: pytest.MonkeyPatch, internal: list[UUID]
) -> None:
    """A thread cannot be stopped, so the lock's transaction stays open until
    the sync really ends: a redelivery cannot write beside it."""
    stub_reads(monkeypatch, internal)
    release, started = threading.Event(), threading.Event()
    ended: list[str] = []

    def slow_sync(*_: Any, **__: Any) -> Any:
        started.set()
        release.wait(5)
        return sync_run.Plan()

    class Session(FakeSession):
        async def __aexit__(self, *_: object) -> None:
            ended.append("closed")

    monkeypatch.setattr(sync_run, "sync", slow_sync)
    task = asyncio.create_task(sync_run.sync_dashboards(Session, FakePostHog().admin(), apply=True))
    await asyncio.to_thread(started.wait, 5)

    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert ended == []

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The transaction, then the session: both only after the thread ended.
    assert ended == ["closed", "closed"]

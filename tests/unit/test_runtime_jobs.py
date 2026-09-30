"""The dispatcher is the one surface every runtime trigger calls."""

from __future__ import annotations

import pytest

from app.infra.jobs.runner import JobResult, RuntimeJobs, UnknownRuntimeJobError


class RecordingJobs(RuntimeJobs):
    def __init__(self) -> None:
        self.called: list[tuple[str, str | None, bool, str | None]] = []

    async def _run_named(
        self, name: str, *, job_id: str | None, dry_run: bool, message_id: str | None
    ) -> JobResult:
        self.called.append((name, job_id, dry_run, message_id))
        return JobResult(name=name, job_id=job_id, status="completed", counts={"changed": 0})


@pytest.mark.asyncio
async def test_all_seven_names_dispatch_through_the_same_runner_surface() -> None:
    jobs = RecordingJobs()
    names = (
        "settle-sessions",
        "credit-reminders",
        "monthly-credits",
        "expire-credits",
        "sync-institutions",
        "refresh-next-available",
        "sweep-intake-files",
    )

    for name in names:
        result = await jobs.run(name, job_id=f"job-{name}", message_id="msg-1")
        assert result.status == "completed"

    assert [call[0] for call in jobs.called] == list(names)


@pytest.mark.asyncio
async def test_an_unknown_job_is_refused_before_dispatch() -> None:
    jobs = RecordingJobs()

    with pytest.raises(UnknownRuntimeJobError):
        await jobs.run("invented", job_id="job-invented")

    assert jobs.called == []


@pytest.mark.asyncio
async def test_the_refresh_job_runs_on_the_configured_booking_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5: the job reads its window from its own settings, like a request
    does, so a lowered maximum reaches next-available too."""
    from typing import Any

    from app.core.config import Settings
    from app.domain.availability import BookingWindow
    from app.infra.jobs import runner

    seen: dict[str, Any] = {}

    class Engine:
        async def dispose(self) -> None:
            return None

    class AsyncNull:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    async def fake_refresh(_session: object, **kwargs: Any) -> dict[str, int]:
        seen.update(kwargs)
        return {"refreshed": 0}

    monkeypatch.setattr(runner, "create_database_engine", lambda _s: Engine())
    monkeypatch.setattr(runner, "create_session_factory", lambda _e: lambda: AsyncNull())
    monkeypatch.setattr(runner, "free_busy_reader", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "refresh_next_available", fake_refresh)
    settings = Settings(_env_file=None, max_booking_window_days=9, default_booking_window_days=4)

    await runner.RuntimeJobs(settings)._refresh_next_available(dry_run=True)

    assert seen["window"] == BookingWindow(max_days=9, default_days=4)

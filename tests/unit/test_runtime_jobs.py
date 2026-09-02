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
async def test_all_five_names_dispatch_through_the_same_runner_surface() -> None:
    jobs = RecordingJobs()
    names = (
        "settle-sessions",
        "credit-reminders",
        "monthly-credits",
        "expire-credits",
        "sync-institutions",
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

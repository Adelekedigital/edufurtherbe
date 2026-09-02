"""Every recovery CLI and HTTP endpoint share the RuntimeJobs entrypoint."""

from __future__ import annotations

import argparse
from importlib import import_module
from typing import Any

import pytest

from app.infra.jobs.runner import JobResult


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "job_name"),
    [
        ("scripts.settle_sessions", "settle-sessions"),
        ("scripts.remind_expiring_credits", "credit-reminders"),
        ("scripts.grant_monthly_credits", "monthly-credits"),
        ("scripts.expire_credits", "expire-credits"),
        ("scripts.sync_institutions", "sync-institutions"),
    ],
)
async def test_each_cli_calls_the_shared_runner(
    monkeypatch: pytest.MonkeyPatch, module_name: str, job_name: str
) -> None:
    module = import_module(module_name)
    calls: list[tuple[str, bool]] = []

    class FakeJobs:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, name: str, *, dry_run: bool = False, **_kwargs: Any) -> JobResult:
            calls.append((name, dry_run))
            return JobResult(name=name, job_id=None, status="no-op")

    monkeypatch.setattr(module, "RuntimeJobs", FakeJobs)
    args = argparse.Namespace(dry_run=True, from_file=None)

    assert await module.run(args) == 0
    assert calls == [(job_name, True)]

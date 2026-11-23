"""A stand-in for QStash, shared by every suite that schedules a callback.
One copy (non-negotiable #8)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.infra.clients.scheduler import SchedulerError


@dataclass
class FakeScheduler:
    """Records what it was asked to publish; ``fails`` makes it raise
    `SchedulerError`, the error the adapters raise and the callers catch."""

    published: list[dict[str, Any]] = field(default_factory=list)
    fails: bool = False

    def schedule(self, *, url: str, body: dict[str, Any], at: dt.datetime) -> None:
        if self.fails:
            raise SchedulerError("qstash said no")
        self.published.append({"url": url, "body": body, "at": at})

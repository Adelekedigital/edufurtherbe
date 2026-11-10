"""The one-off analytics history backfill (PRD 30 PR 7): run by
`scripts/backfill_analytics.py`, which only wires it up.

Sends, for every stored fact from before live analytics began, the event the
live writer would have queued for it: same name, same person (`users.id`),
same properties, at the fact's own time. Facts are read kind by kind
(`infra/db/analytics_backfill`), each kind in time order.

- **The cutoff** is ``until``, always given by the operator: when the server
  events started being queued (the production deploy of the server-events
  PR). Only facts strictly before it are sent; from then on every fact has its
  live event. It is never derived from the outbox, whose analytics rows can be
  purged (`forget_unsent_analytics`): that would move the cutoff and send
  facts twice under different ids. The same ``until`` on every run keeps a
  rerun's ids identical.
- **Each event's `uuid` is derived from its fact** (`history_event_id`), so a
  re-run or an overlapping run is dropped by PostHog rather than counted twice.
- **Sent with `historical_migration`**, which PostHog requires of imported
  history (https://posthog.com/docs/migrate, "To capture events, you must use
  ... the PostHog API `batch` endpoint with the `historical_migration` set to
  `true`"), in batches of at most `BATCH`.
- **A cursor file** records the last fact sent, written after each batch is
  taken, so a stopped run continues where it stopped (PostHog advises a
  resumable, cursor-based import, same page).
- **A dry run** (no sink) counts and builds every event, sends nothing and
  writes no cursor.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, analytics_gaps, env_key
from app.domain.analytics_events import (
    AnalyticsEvent,
    cancelled_properties,
    granted_properties,
    historical_event,
    requested_properties,
    review_properties,
    session_event_names,
    session_properties,
    spent_properties,
)
from app.domain.enums import CreditReason
from app.domain.ports import AnalyticsSink
from app.infra.clients.posthog import PostHogSink
from app.infra.db import analytics_backfill as facts

__all__ = [
    "BATCH",
    "KINDS",
    "BackfillRefusedError",
    "BackfillReport",
    "backfill_analytics",
    "historical_sink",
]

#: Events per request.
BATCH = 100

#: How many built events a dry run shows.
SAMPLE = 3

Event = dict[str, Any]
Reader = Callable[..., AsyncIterator[RowMapping]]


class BackfillRefusedError(RuntimeError):
    """The backfill will not run as asked; nothing was sent."""


def _session_events(row: RowMapping) -> list[Event]:
    built = []
    for name in session_event_names(
        row["from_status"], row["to_status"], moved_from=row["moved_from"], moved_on=row["moved_on"]
    ):
        if name is AnalyticsEvent.BOOKING_REQUESTED:
            properties = requested_properties(row["session_id"], row["session_type_id"])
        elif name is AnalyticsEvent.SESSION_CANCELLED:
            properties = cancelled_properties(row["session_id"], row["by"], row["reason_code"])
        else:
            properties = session_properties(row["session_id"])
        built.append(_event(name, row, properties))
    return built


def _credit_events(row: RowMapping) -> list[Event]:
    if row["reason"] == CreditReason.GRANT:
        return [
            _event(
                AnalyticsEvent.CREDIT_GRANTED, row, granted_properties(row["delta"], row["source"])
            )
        ]
    return [
        _event(AnalyticsEvent.CREDIT_SPENT, row, spent_properties(-row["delta"], row["session_id"]))
    ]


def _listing_events(row: RowMapping) -> list[Event]:
    return [_event(AnalyticsEvent.MENTOR_LISTED, row, {})]


def _review_events(row: RowMapping) -> list[Event]:
    return [_event(AnalyticsEvent.REVIEW_SUBMITTED, row, review_properties(row["rating"]))]


def _unlock_events(row: RowMapping) -> list[Event]:
    return [_event(AnalyticsEvent.REFERRAL_UNLOCKED, row, {})]


def _event(name: AnalyticsEvent, row: RowMapping, properties: dict[str, object]) -> Event:
    return historical_event(
        name, fact_id=row["id"], user_id=row["user_id"], at=row["at"], properties=properties
    )


#: Each kind of fact, in the order the backfill reads them, with its reader and
#: what it reports. The cursor names a kind by its key.
KINDS: dict[str, tuple[Reader, Callable[[RowMapping], list[Event]]]] = {
    "session_events": (facts.session_event_facts, _session_events),
    "credit_transactions": (facts.credit_facts, _credit_events),
    "mentor_status_events": (facts.listing_facts, _listing_events),
    "reviews": (facts.review_facts, _review_events),
    "referral_unlocks": (facts.unlock_facts, _unlock_events),
}


@dataclass
class BackfillReport:
    """What a run found, built and sent. ``sample`` is the first events built."""

    cutoff: dt.datetime
    counts: dict[str, int] = field(default_factory=dict)
    sample: list[Event] = field(default_factory=list)
    sent: int = 0


def historical_sink(settings: Settings, *, client: httpx.Client | None = None) -> AnalyticsSink:
    """PostHog, marked as a historical migration, or a refusal naming what is
    missing: analytics off, or `analytics_gaps`."""
    if not settings.analytics_enabled:
        raise BackfillRefusedError(f"{env_key('analytics_enabled')} is off")
    if gaps := analytics_gaps(settings):
        raise BackfillRefusedError("analytics is not configured: " + "; ".join(gaps))
    host, token = settings.analytics_host, settings.analytics_project_token
    if not (host and token):  # `analytics_gaps` decided; this narrows the types.
        raise BackfillRefusedError("analytics is not configured")
    return PostHogSink(host=host, project_token=token, historical_migration=True, client=client)


def _read_cursor(path: Path) -> tuple[str, dt.datetime, UUID] | None:
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved["kind"] not in KINDS:
        raise BackfillRefusedError(f"the cursor names no kind of fact: {saved['kind']!r}")
    return saved["kind"], dt.datetime.fromisoformat(saved["at"]), UUID(saved["id"])


def _write_cursor(path: Path, kind: str, row: RowMapping) -> None:
    """Replaced whole, so a run stopped mid-write leaves the last good cursor."""
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(
        json.dumps({"kind": kind, "at": row["at"].isoformat(), "id": str(row["id"])}),
        encoding="utf-8",
    )
    partial.replace(path)


async def backfill_analytics(
    engine: AsyncEngine,
    *,
    sink: AnalyticsSink | None,
    cursor: Path,
    until: dt.datetime,
    batch: int = BATCH,
) -> BackfillReport:
    """Send (or, with no ``sink``, count) every past fact's event before
    ``until``, from where ``cursor`` says the last run stopped."""
    async with AsyncSession(engine) as session:
        report = BackfillReport(cutoff=until)
        counts: Counter[str] = Counter()
        resume = _read_cursor(cursor)
        kinds = list(KINDS)
        start = kinds.index(resume[0]) if resume else 0

        for kind in kinds[start:]:
            read, build = KINDS[kind]
            after = resume[1:] if resume and kind == resume[0] else None
            pending: list[Event] = []
            last: RowMapping | None = None
            async for row in read(session, until=until, after=after):
                built = build(row)
                if pending and len(pending) + len(built) > batch:
                    await _send(sink, pending, cursor, kind, last, report)
                    pending = []
                for event in built:
                    counts[event["event"]] += 1
                    if len(report.sample) < SAMPLE:
                        report.sample.append(event)
                pending.extend(built)
                last = row
            if pending:
                await _send(sink, pending, cursor, kind, last, report)

    report.counts = {event.value: counts[event.value] for event in AnalyticsEvent}
    return report


async def _send(
    sink: AnalyticsSink | None,
    events: list[Event],
    cursor: Path,
    kind: str,
    last: RowMapping | None,
    report: BackfillReport,
) -> None:
    """One batch out, then the cursor past it. Nothing at all on a dry run."""
    if sink is None or last is None:
        return
    await asyncio.to_thread(sink.capture, events)
    _write_cursor(cursor, kind, last)
    report.sent += len(events)

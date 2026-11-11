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
  rerun's ids identical. **It stays the first live deploy's instant for
  ever** (decision 330): a rerun with a later ``until`` also sends the facts
  between the two, which were already sent live under their outbox ids, so
  PostHog counts them twice.
- **Each event's `uuid` is derived from its fact** (`history_event_id`), so a
  re-run or an overlapping run is dropped by PostHog rather than counted twice.
- **Sent with `historical_migration`**, which PostHog requires of imported
  history (https://posthog.com/docs/migrate, "To capture events, you must use
  ... the PostHog API `batch` endpoint with the `historical_migration` set to
  `true`"), in batches of at most `BATCH`.
- **A cursor file** records the last fact sent, written after each batch is
  taken, so a stopped run continues where it stopped (PostHog advises a
  resumable, cursor-based import, same page). It also records ``until`` and
  the target (`target_fingerprint`), and a resume with either changed is
  refused: it would skip facts never sent to that project, or mix cutoffs.
  It carries `CURSOR_FORMAT`, and a cursor of another format is refused too:
  one written before the mentor's side (decision 337) would resume past
  facts whose mentor events were never sent.
- **A dry run** (no sink) counts and builds every event, sends nothing and
  writes no cursor.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
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
    MENTOR_COUNTERPARTS,
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
from app.infra.http.upstream import trim_origin

__all__ = [
    "BATCH",
    "CURSOR_FORMAT",
    "KINDS",
    "MAX_EVENTS_PER_FACT",
    "BackfillRefusedError",
    "BackfillReport",
    "Cursor",
    "backfill_analytics",
    "historical_sink",
    "read_cursor",
    "target_fingerprint",
]

#: Events per request.
BATCH = 100

#: The most events one fact reports: a request booked outright (requested,
#: received, confirmed) or a legacy completion (confirmed, completed,
#: delivered). A fact's events are never split, so the cursor always points at
#: a fact sent whole, and a smaller batch is refused.
MAX_EVENTS_PER_FACT = 3

#: The cursor's format, bumped whenever a fact starts reporting more events:
#: a cursor of another format would resume past facts sent without them.
#: 2 is decision 337 (the mentor's side).
CURSOR_FORMAT = 2

#: How many built events a dry run shows.
SAMPLE = 3

Event = dict[str, Any]
Reader = Callable[..., AsyncIterator[RowMapping]]


class BackfillRefusedError(RuntimeError):
    """The backfill will not run as asked; nothing was sent."""


def _session_events(row: RowMapping) -> list[Event]:
    """What a `session_events` row reports, after the legacy confirmation its
    ending carries (``confirmed_at``, `facts.session_event_facts`): that one is
    dated by the request and keyed by this row, the only fact it has.

    Each event is about the mentee, followed by its mentor's counterpart
    (`MENTOR_COUNTERPARTS`, decision 337) when it has one; each side only when
    `_sides` names that person, as the live writers skip them."""
    mentee, mentor = _sides(row)
    built = []
    if row["confirmed_at"] is not None and mentee is not None:
        confirmed = session_properties(row["session_id"], row["entry_point"])
        built.append(
            _event(
                AnalyticsEvent.SESSION_CONFIRMED,
                row,
                confirmed,
                about=mentee,
                at=row["confirmed_at"],
            )
        )
    for name in session_event_names(
        row["from_status"], row["to_status"], moved_from=row["moved_from"], moved_on=row["moved_on"]
    ):
        if name is AnalyticsEvent.BOOKING_REQUESTED:
            properties = requested_properties(
                row["session_id"], row["session_type_id"], row["entry_point"]
            )
        elif name is AnalyticsEvent.SESSION_CANCELLED:
            properties = cancelled_properties(
                row["session_id"], row["by"], row["reason_code"], row["entry_point"]
            )
        else:
            properties = session_properties(row["session_id"], row["entry_point"])
        if mentee is not None:
            built.append(_event(name, row, properties, about=mentee))
        counterpart = MENTOR_COUNTERPARTS.get(name)
        if counterpart is not None and mentor is not None:
            built.append(_event(counterpart, row, properties, about=mentor))
    return built


def _sides(row: RowMapping) -> tuple[UUID | None, UUID | None]:
    """A session row's mentee and mentor, each ``None`` when no event may be
    sent about them (`analytics_scope.sendable`, read by the reader): the one
    place a session row's people are checked, so no builder sends about an
    excluded one."""
    return (
        row["mentee_id"] if row["mentee_counted"] else None,
        row["mentor_id"] if row["mentor_counted"] else None,
    )


def _credit_events(row: RowMapping) -> list[Event]:
    if row["reason"] == CreditReason.GRANT:
        return [
            _event(
                AnalyticsEvent.CREDIT_GRANTED,
                row,
                granted_properties(row["delta"], row["source"]),
                about=row["user_id"],
            )
        ]
    return [
        _event(
            AnalyticsEvent.CREDIT_SPENT,
            row,
            spent_properties(-row["delta"], row["session_id"]),
            about=row["user_id"],
        )
    ]


def _listing_events(row: RowMapping) -> list[Event]:
    return [_event(AnalyticsEvent.MENTOR_LISTED, row, {}, about=row["user_id"])]


def _review_events(row: RowMapping) -> list[Event]:
    return [
        _event(
            AnalyticsEvent.REVIEW_SUBMITTED,
            row,
            review_properties(row["rating"]),
            about=row["user_id"],
        )
    ]


def _unlock_events(row: RowMapping) -> list[Event]:
    return [_event(AnalyticsEvent.REFERRAL_UNLOCKED, row, {}, about=row["user_id"])]


def _event(
    name: AnalyticsEvent,
    row: RowMapping,
    properties: dict[str, object],
    *,
    about: UUID,
    at: dt.datetime | None = None,
) -> Event:
    """``row``'s event about ``about``, always named by the caller (a reader
    has already left out whom no event may be about), at the row's own time
    unless ``at`` says otherwise."""
    return historical_event(
        name, fact_id=row["id"], user_id=about, at=at or row["at"], properties=properties
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


def target_fingerprint(settings: Settings) -> str | None:
    """Which PostHog project a run sends to, without the token: the sha256 of
    host and project token, or ``None`` while either is unset."""
    host, token = settings.analytics_host, settings.analytics_project_token
    if not (host and token):
        return None
    return hashlib.sha256(f"{trim_origin(host)}\n{token}".encode()).hexdigest()


@dataclass(frozen=True)
class Cursor:
    """The last fact a run sent: its kind and ``(at, id)`` position."""

    kind: str
    at: dt.datetime
    id: UUID


def read_cursor(path: Path, *, until: dt.datetime, target: str | None) -> Cursor | None:
    """The cursor at ``path``, or ``None`` when there is none. Refused when it
    cannot be read whole, is of another `CURSOR_FORMAT`, or was written for
    another ``until`` or target (the target is compared only when known: a dry
    run may have none)."""
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        cursor = Cursor(saved["kind"], dt.datetime.fromisoformat(saved["at"]), UUID(saved["id"]))
        written_until = dt.datetime.fromisoformat(saved["until"])
        written_target = saved["target"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackfillRefusedError(f"the cursor {path} is unreadable ({exc!r})") from exc
    if saved.get("format") != CURSOR_FORMAT:
        raise BackfillRefusedError(
            f"the cursor {path} predates mentor-side events (decision 337); "
            "start the run again without it"
        )
    if cursor.kind not in KINDS:
        raise BackfillRefusedError(f"the cursor names no kind of fact: {cursor.kind!r}")
    if written_until != until:
        raise BackfillRefusedError(
            f"the cursor {path} was written for --until {written_until.isoformat()}, "
            f"not {until.isoformat()}"
        )
    if target is not None and written_target != target:
        raise BackfillRefusedError(
            f"the cursor {path} was written for another PostHog host or project"
        )
    return cursor


@dataclass(frozen=True)
class Position:
    """Where a batch leaves the run: the cursor file and what it records."""

    path: Path
    kind: str
    last: RowMapping | None
    until: dt.datetime
    target: str | None


def _write_cursor(position: Position, last: RowMapping) -> None:
    """Replaced whole, so a run stopped mid-write leaves the last good cursor."""
    partial = position.path.with_name(position.path.name + ".tmp")
    partial.write_text(
        json.dumps(
            {
                "kind": position.kind,
                "at": last["at"].isoformat(),
                "id": str(last["id"]),
                "until": position.until.isoformat(),
                "target": position.target,
                "format": CURSOR_FORMAT,
            }
        ),
        encoding="utf-8",
    )
    partial.replace(position.path)


async def backfill_analytics(
    engine: AsyncEngine,
    *,
    sink: AnalyticsSink | None,
    cursor: Path,
    until: dt.datetime,
    target: str | None,
    batch: int = BATCH,
) -> BackfillReport:
    """Send (or, with no ``sink``, count) every past fact's event before
    ``until``, from where ``cursor`` says the last run stopped. ``target`` is
    `target_fingerprint`'s, required to send."""
    if batch < MAX_EVENTS_PER_FACT:
        raise ValueError(f"batch must hold one fact's events: at least {MAX_EVENTS_PER_FACT}")
    if sink is not None and target is None:
        raise BackfillRefusedError("sending needs the target PostHog host and project")
    resume = read_cursor(cursor, until=until, target=target)
    async with AsyncSession(engine) as session:
        report = BackfillReport(cutoff=until)
        counts: Counter[str] = Counter()
        kinds = list(KINDS)
        start = kinds.index(resume.kind) if resume else 0

        for kind in kinds[start:]:
            read, build = KINDS[kind]
            after = (resume.at, resume.id) if resume and kind == resume.kind else None
            pending: list[Event] = []
            last: RowMapping | None = None
            async for row in read(session, until=until, after=after):
                built = build(row)
                if pending and len(pending) + len(built) > batch:
                    await _send(sink, pending, report, Position(cursor, kind, last, until, target))
                    pending = []
                for event in built:
                    counts[event["event"]] += 1
                    if len(report.sample) < SAMPLE:
                        report.sample.append(event)
                pending.extend(built)
                last = row
            if pending:
                await _send(sink, pending, report, Position(cursor, kind, last, until, target))

    report.counts = {event.value: counts[event.value] for event in AnalyticsEvent}
    return report


async def _send(
    sink: AnalyticsSink | None,
    events: list[Event],
    report: BackfillReport,
    position: Position,
) -> None:
    """One batch out, then the cursor past it. Nothing at all on a dry run."""
    if sink is None or position.last is None or position.target is None:
        return
    await asyncio.to_thread(sink.capture, events)
    _write_cursor(position, position.last)
    report.sent += len(events)

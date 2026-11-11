"""Analytics events in the outbox (PRD 30 PR 1): queued, swept on their own, sent in batches.

**Each drain selects its own destinations.** The email drain selected every row
that was not a push, so an analytics row would have been read as a message: no
`recipient_id`, a `KeyError`, and the whole sweep aborted for everybody.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_admin_credits import a_user
from tests.integration.test_api_notifications import Recorder
from tests.integration.test_api_push_messages import FakePush

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.domain.analytics_events import AnalyticsEvent, analytics_event
from app.domain.notifications import Channel, Notification
from app.infra.db.outbox import (
    BATCH,
    MAX_ATTEMPTS,
    drain,
    drain_analytics,
    drain_push,
    enqueue_analytics,
)
from app.infra.jobs import runner
from app.infra.jobs.runner import RuntimeJobs
from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

AT = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.UTC)


class FakeSink:
    """The analytics vendor: each batch it was handed, or a refusal."""

    def __init__(self, *, down: bool = False) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.down = down

    def capture(self, events: list[dict[str, Any]]) -> None:
        if self.down:
            raise UpstreamError("analytics capture failed: 503")
        self.batches.append(events)


async def queue(engine: AsyncEngine, count: int = 1, *, user: UUID | None = None) -> list[UUID]:
    """`count` events queued, each in its own transaction, oldest first, about
    ``user`` or one new account (an event about nobody is never queued)."""
    user = user or await a_user(engine)
    ids = []
    for index in range(count):
        async with async_sessionmaker(engine)() as session:
            await enqueue_analytics(
                session,
                AnalyticsEvent.CREDIT_SPENT,
                user,
                {"amount": index},
                at=AT + dt.timedelta(seconds=index),
            )
            ids.append(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM outbox_events ORDER BY created_at DESC, id DESC LIMIT 1"
                        )
                    )
                ).scalar_one()
            )
            await session.commit()
    return ids


async def rows_in(session: Any) -> list[Any]:
    found = await session.execute(
        text("SELECT id FROM outbox_events WHERE destination = 'analytics'")
    )
    return list(found.all())


async def rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT id, event_type, entity_type, entity_id, destination, payload, status, "
                "attempts, error_detail, sent_at FROM outbox_events "
                "WHERE destination = 'analytics' ORDER BY created_at, id"
            )
        )
        return [dict(row) for row in found.mappings()]


async def run(engine: AsyncEngine, sink: FakeSink, now: dt.datetime = AT) -> dict[str, int]:
    async with async_sessionmaker(engine)() as session:
        counts = await drain_analytics(session, sink=sink, now=now)
        await session.commit()
    return counts


# --------------------------------------------------------------------------
# The enqueue
# --------------------------------------------------------------------------


async def test_an_event_is_one_analytics_row_carrying_the_batch_shape(
    db_engine: AsyncEngine,
) -> None:
    user = await a_user(db_engine)
    await queue(db_engine, user=user)

    (row,) = await rows(db_engine)

    assert row["destination"] == Channel.ANALYTICS
    assert (row["event_type"], row["entity_type"], row["entity_id"]) == (
        "credit_spent",
        "user",
        user,
    )
    assert row["payload"] == analytics_event(
        AnalyticsEvent.CREDIT_SPENT, user_id=user, at=AT, properties={"amount": 0}
    )
    assert (row["status"], row["attempts"]) == ("pending", 0)


async def test_an_event_rolls_back_with_the_transaction_that_queued_it(
    db_engine: AsyncEngine,
) -> None:
    async with async_sessionmaker(db_engine)() as session:
        await enqueue_analytics(
            session, AnalyticsEvent.MENTOR_LISTED, await a_user(db_engine), {}, at=AT
        )
        assert len(await rows_in(session)) == 1
        await session.rollback()

    assert await rows(db_engine) == []


# --------------------------------------------------------------------------
# Each drain sweeps its own destinations
# --------------------------------------------------------------------------


async def test_the_email_and_push_drains_never_touch_an_analytics_row(
    db_engine: AsyncEngine,
) -> None:
    """The latent bug, and the positive case beside it: the email drain still
    sends an email row and still reaches WhatsApp, while the analytics row
    waits, untouched, for its own drain."""
    async with db_engine.begin() as conn:
        person = await add_user(conn, "sweep-sep@example.test", first_name="Sam")
        for channel in (Channel.EMAIL, Channel.WHATSAPP):
            await conn.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_type, entity_type, entity_id, destination, payload) "
                    "VALUES (:e, 'user', :u, :d, CAST(:p AS jsonb))"
                ),
                {
                    "e": Notification.CREDITS_EXPIRING,
                    "u": person,
                    "d": channel,
                    "p": f'{{"recipient_id": "{person}"}}',
                },
            )
    await queue(db_engine)
    recorder, push = Recorder(), FakePush()

    async with async_sessionmaker(db_engine)() as session:
        emailed = await drain(session, notifier=recorder, now=AT)
        pushed = await drain_push(session, web_push=push, now=AT)
        await session.commit()

    assert emailed == {"sent": 1, "failed": 0, "skipped": 1}
    assert [message["channel"] for message in recorder.sent] == [Channel.EMAIL]
    assert pushed == {"sent": 0, "failed": 0, "skipped": 0}
    (row,) = await rows(db_engine)
    assert (row["status"], row["attempts"], row["error_detail"]) == ("pending", 0, None)


async def test_the_analytics_drain_never_touches_an_email_row(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        person = await add_user(conn, "only-analytics@example.test")
        await conn.execute(
            text(
                "INSERT INTO outbox_events "
                "(event_type, entity_type, entity_id, destination, payload) "
                "VALUES ('credits_expiring', 'user', :u, 'email', CAST(:p AS jsonb))"
            ),
            {"u": person, "p": f'{{"recipient_id": "{person}"}}'},
        )
    sink = FakeSink()

    assert await run(db_engine, sink) == {"sent": 0, "failed": 0}

    assert sink.batches == []
    async with db_engine.connect() as conn:
        status, attempts = (
            await conn.execute(
                text("SELECT status, attempts FROM outbox_events WHERE destination = 'email'")
            )
        ).one()
    assert (status, attempts) == ("pending", 0)


# --------------------------------------------------------------------------
# The analytics drain
# --------------------------------------------------------------------------


async def test_pending_events_go_in_batches_of_a_hundred_oldest_first(
    db_engine: AsyncEngine,
) -> None:
    ids = await queue(db_engine, BATCH + 1)
    sink = FakeSink()

    counts = await run(db_engine, sink)

    assert counts == {"sent": BATCH + 1, "failed": 0}
    assert [len(batch) for batch in sink.batches] == [BATCH, 1]
    sent = [event for batch in sink.batches for event in batch]
    assert [event["properties"]["amount"] for event in sent] == list(range(BATCH + 1))
    found = await rows(db_engine)
    assert [row["id"] for row in found] == ids
    assert {(row["status"], row["attempts"], row["sent_at"]) for row in found} == {("sent", 1, AT)}


async def test_each_event_is_sent_with_its_row_id_as_its_uuid(db_engine: AsyncEngine) -> None:
    """A batch resent after a lost answer carries the same `uuid`s, so the
    vendor drops the copies (decision 137); the stored payload is unchanged."""
    ids = await queue(db_engine, 3)
    sink = FakeSink()

    await run(db_engine, sink)

    (batch,) = sink.batches
    assert [event["uuid"] for event in batch] == [str(row_id) for row_id in ids]
    assert [event["properties"]["amount"] for event in batch] == [0, 1, 2]
    assert all("uuid" not in row["payload"] for row in await rows(db_engine))


async def test_a_sent_event_is_not_sent_again(db_engine: AsyncEngine) -> None:
    await queue(db_engine, 2)
    await run(db_engine, FakeSink())
    sink = FakeSink()

    assert await run(db_engine, sink) == {"sent": 0, "failed": 0}
    assert sink.batches == []


async def test_a_refused_batch_is_retried_and_stops_the_run(db_engine: AsyncEngine) -> None:
    """One attempt counted per row, the error kept, the row back to pending for
    the next sweep; and the run sends no further batch into a vendor that is
    down."""
    await queue(db_engine, BATCH + 1)
    sink = FakeSink(down=True)

    counts = await run(db_engine, sink)

    assert counts == {"sent": 0, "failed": BATCH}
    found = await rows(db_engine)
    assert [(row["status"], row["attempts"]) for row in found] == [("pending", 1)] * BATCH + [
        ("pending", 0)
    ]
    assert found[0]["error_detail"] == "analytics capture failed: 503"

    sink.down = False
    assert await run(db_engine, sink) == {"sent": BATCH + 1, "failed": 0}


async def test_an_event_refused_max_attempts_times_stays_failed_with_its_error(
    db_engine: AsyncEngine,
) -> None:
    await queue(db_engine)
    sink = FakeSink(down=True)

    for _ in range(MAX_ATTEMPTS):
        await run(db_engine, sink)
    sink.down = False
    counts = await run(db_engine, sink)

    assert counts == {"sent": 0, "failed": 0}
    (row,) = await rows(db_engine)
    assert (row["status"], row["attempts"], row["error_detail"]) == (
        "failed",
        MAX_ATTEMPTS,
        "analytics capture failed: 503",
    )


@pytest.mark.parametrize("dry_run", [False, True])
async def test_the_hourly_sweep_sends_events_when_analytics_is_on(
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    await queue(db_engine)
    sink = FakeSink()
    monkeypatch.setattr(runner, "live_analytics_sink", lambda _settings: sink)

    result = await RuntimeJobs(
        Settings(_env_file=None, database_url=SecretStr(migrated_database))  # type: ignore[call-arg]
    ).run("settle-sessions", dry_run=dry_run)

    assert len(sink.batches) == (0 if dry_run else 1)
    assert result.counts["analytics_sent"] == (0 if dry_run else 1)
    (row,) = await rows(db_engine)
    assert row["status"] == ("pending" if dry_run else "sent")


async def test_with_analytics_off_the_hourly_sweep_leaves_events_waiting(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    await queue(db_engine)

    result = await RuntimeJobs(
        Settings(_env_file=None, database_url=SecretStr(migrated_database))  # type: ignore[call-arg]
    ).run("settle-sessions", dry_run=False)

    assert result.counts["analytics_sent"] == 0
    (row,) = await rows(db_engine)
    assert (row["status"], row["attempts"]) == ("pending", 0)

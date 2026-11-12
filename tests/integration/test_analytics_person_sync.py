"""The one-off PostHog person sync (decision 340): one `$set` event per
sendable person, never one about an internal or deleted account."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor
from tests.integration.test_api_admin_credits import a_user

from app.infra.analytics_person_sync import sync_person_properties

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class FakeSink:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def capture(self, events: list[dict[str, Any]]) -> None:
        self.batches.append(list(events))


async def people(engine: AsyncEngine) -> dict[str, Any]:
    return {
        "mentees": [str(await a_user(engine)) for _ in range(3)],
        "mentor": str(await make_public_mentor(engine, "sync", approved=False)),
        "internal": str(await a_user(engine, role="limited_access")),
        "deleted": str(await a_user(engine, deleted=True)),
    }


async def test_a_dry_run_builds_one_update_per_sendable_person_and_sends_nothing(
    db_engine: AsyncEngine,
) -> None:
    await people(db_engine)
    sink = FakeSink()
    sent = await sync_person_properties(db_engine, sink=sink, batch=2)

    report = await sync_person_properties(db_engine, sink=None, batch=2)

    assert (report.sent, report.built) == (0, sent.built)
    assert report.built >= 4
    assert report.sample is not None
    assert report.sample["event"] == "$set"
    assert report.sample["properties"]["$set"]["user_id"] == report.sample["distinct_id"]
    assert "account_created_month" in report.sample["properties"]["$set_once"]


async def test_send_posts_every_sendable_person_once_in_batches(db_engine: AsyncEngine) -> None:
    who = await people(db_engine)
    sink = FakeSink()

    report = await sync_person_properties(db_engine, sink=sink, batch=2)

    sent = [event for batch in sink.batches for event in batch]
    ids = [event["distinct_id"] for event in sent]
    assert all(len(batch) <= 2 for batch in sink.batches) and len(sink.batches) >= 2
    assert len(ids) == len(set(ids)) == report.sent == report.built
    assert {*who["mentees"], who["mentor"]} <= set(ids)
    assert who["internal"] not in ids and who["deleted"] not in ids
    mentor = next(event for event in sent if event["distinct_id"] == who["mentor"])
    assert mentor["properties"]["$set"]["mentor_status"] == "pending"
    assert mentor["properties"]["$geoip_disable"] is True

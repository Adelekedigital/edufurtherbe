"""The one-off PostHog person sync (decision 340): one `$set` event per
sendable person PostHog already knows, never one about an internal or deleted
account, and never one that would create a person."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor
from tests.integration.test_api_admin_credits import a_user

from app.core.errors import UpstreamError
from app.infra.analytics_person_sync import SyncReport, sync_person_properties

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class FakeSink:
    def __init__(self, *, down_from: int | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.down_from = down_from

    def capture(self, events: list[dict[str, Any]]) -> None:
        if self.down_from is not None and len(self.batches) + 1 >= self.down_from:
            raise UpstreamError("analytics capture failed: 503")
        self.batches.append(list(events))

    @property
    def ids(self) -> list[str]:
        return [event["distinct_id"] for batch in self.batches for event in batch]


class FakeReader:
    """PostHog knowing ``known`` (everyone when ``None``); every id it was asked about."""

    def __init__(self, known: set[UUID] | None = None) -> None:
        self.known = known
        self.asked: list[list[UUID]] = []
        self.internal: list[UUID] = []

    def known_persons(self, user_ids: Sequence[UUID], internal: Sequence[UUID]) -> set[UUID]:
        assert set(user_ids).isdisjoint(internal)
        self.internal = list(internal)
        self.asked.append(list(user_ids))
        return set(user_ids) if self.known is None else set(user_ids) & self.known


async def people(engine: AsyncEngine) -> dict[str, Any]:
    return {
        "mentees": [await a_user(engine) for _ in range(3)],
        "mentor": await make_public_mentor(engine, "sync", approved=False),
        "internal": await a_user(engine, role="limited_access"),
        "deleted": await a_user(engine, deleted=True),
    }


async def test_a_dry_run_counts_the_known_sendable_people_and_sends_nothing(
    db_engine: AsyncEngine,
) -> None:
    who = await people(db_engine)
    reader = FakeReader({who["mentees"][0], who["mentor"], who["internal"]})

    report = await sync_person_properties(db_engine, reader=reader, sink=None, batch=2)

    asked = {user for page in reader.asked for user in page}
    assert {*who["mentees"], who["mentor"]} <= asked
    assert who["internal"] not in asked and who["deleted"] not in asked
    assert who["internal"] in reader.internal
    assert (report.sendable, report.known, report.sent) == (len(asked), 2, 0)
    assert report.sample is not None
    assert report.sample["event"] == "$set"
    assert report.sample["properties"]["$set"]["user_id"] == report.sample["distinct_id"]


async def test_send_updates_only_the_people_posthog_knows_in_batches(
    db_engine: AsyncEngine,
) -> None:
    who = await people(db_engine)
    known = {*who["mentees"][:2], who["mentor"]}
    sink = FakeSink()

    report = await sync_person_properties(db_engine, reader=FakeReader(known), sink=sink, batch=2)

    assert sorted(sink.ids) == sorted(str(user) for user in known)
    assert report.sent == report.known == 3
    assert all(len(batch) <= 2 for batch in sink.batches)
    mentor = next(e for b in sink.batches for e in b if e["distinct_id"] == str(who["mentor"]))
    assert mentor["properties"]["$set"]["mentor_status"] == "pending"
    assert "$unset" not in mentor["properties"]
    assert mentor["properties"]["$geoip_disable"] is True


async def test_a_stopped_run_leaves_what_it_sent_in_the_callers_report(
    db_engine: AsyncEngine,
) -> None:
    await people(db_engine)
    report = SyncReport()

    with pytest.raises(UpstreamError):
        await sync_person_properties(
            db_engine, reader=FakeReader(), sink=FakeSink(down_from=2), report=report, batch=2
        )

    assert report.sent == 2

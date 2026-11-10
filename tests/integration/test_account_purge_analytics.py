"""The account purge erases the person in product analytics (PRD 30 PR 2).

Beside the Loops contact and retried the same way: a refusal leaves the whole
account for the next run, which erases again before anything of ours goes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_account_purge import (
    FakeAdmin,
    FakeStorage,
    a_full_account,
    count,
    deleted,
    purge,
)

from app.core.errors import UpstreamError

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

DAY = dt.timedelta(days=1)


class FakeAnalytics:
    def __init__(self, *, fail: bool = False) -> None:
        self.erased: list[UUID] = []
        self.fail = fail

    def capture(self, events: list[dict[str, Any]]) -> None:
        raise AssertionError(f"the purge sends no events, not {len(events)}")

    def delete_person(self, user_id: UUID) -> None:
        if self.fail:
            raise UpstreamError("analytics person erase failed: 503")
        self.erased.append(user_id)


async def test_a_purged_account_is_erased_in_analytics_by_its_user_id(
    db_engine: AsyncEngine,
) -> None:
    user, _ = await a_full_account(db_engine, "erase")
    await deleted(db_engine, user, purge_in=-DAY)
    analytics = FakeAnalytics()

    counts = await purge(db_engine, FakeAdmin(), FakeStorage(), analytics=analytics)

    assert counts == {"due": 1, "purged": 1, "failed": 0}
    assert analytics.erased == [user]


async def test_an_account_not_yet_due_is_not_erased(db_engine: AsyncEngine) -> None:
    user, _ = await a_full_account(db_engine, "erase-later")
    await deleted(db_engine, user, purge_in=DAY)
    analytics = FakeAnalytics()

    await purge(db_engine, FakeAdmin(), FakeStorage(), analytics=analytics)

    assert analytics.erased == []


async def test_an_analytics_failure_leaves_the_account_for_the_next_run(
    db_engine: AsyncEngine,
) -> None:
    user, auth = await a_full_account(db_engine, "erase-retry")
    await deleted(db_engine, user, purge_in=-DAY)
    admin = FakeAdmin()

    counts = await purge(db_engine, admin, FakeStorage(), analytics=FakeAnalytics(fail=True))

    assert counts == {"due": 1, "purged": 0, "failed": 1}
    assert admin.deleted == [], "the sign-in stays until the erase is taken"
    assert await count(db_engine, "user_profiles", "user_id", user) == 1

    analytics = FakeAnalytics()
    assert (await purge(db_engine, admin, FakeStorage(), analytics=analytics))["purged"] == 1
    assert analytics.erased == [user]
    assert admin.deleted == [auth]


async def test_with_analytics_off_the_purge_still_runs(db_engine: AsyncEngine) -> None:
    user, _ = await a_full_account(db_engine, "erase-off")
    await deleted(db_engine, user, purge_in=-DAY)

    counts = await purge(db_engine, FakeAdmin(), FakeStorage(), analytics=None)

    assert counts == {"due": 1, "purged": 1, "failed": 0}


async def test_the_cookie_answer_goes_with_the_account(db_engine: AsyncEngine) -> None:
    user, _ = await a_full_account(db_engine, "erase-consent")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO analytics_consents (user_id, granted, version) VALUES (:u, true, 'v')"
            ),
            {"u": user},
        )
    await deleted(db_engine, user, purge_in=-DAY)

    await purge(db_engine, FakeAdmin(), FakeStorage(), analytics=FakeAnalytics())

    assert await count(db_engine, "analytics_consents", "user_id", user) == 0

"""A request with no `respond_by` lapses when its session starts.

Migrated legacy requests carry no deadline. Before this rule, "no deadline"
meant "never lapses", so a request whose session had already started counted
as awaiting the mentor forever, was never expired, and could still be accepted.
The deadline is now `COALESCE(respond_by, starts_at)`, in one place
(`pending_requests._past_deadline`).
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_response_deadline import a_request, status_of, sweep

from app.infra.db.pending_requests import booking_counts

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def undated(engine: AsyncEngine, session_id: str, *, started: bool) -> None:
    """Make the request look migrated: pending, no deadline, and optionally begun."""
    back = dt.timedelta(days=30 if started else 0)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET status = 'pending_mentor_approval', respond_by = NULL, "
                "starts_at = starts_at - CAST(:back AS interval) WHERE id = :i"
            ),
            {"i": session_id, "back": back},
        )


async def without_spend(engine: AsyncEngine, session_id: str) -> None:
    """A migrated request never paid through the new ledger: no debit exists."""
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM credit_transactions WHERE session_id = :i"), {"i": session_id}
        )


async def refund_lots(engine: AsyncEngine, session_id: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM credit_transactions "
                        "WHERE session_id = :i AND delta > 0"
                    ),
                    {"i": session_id},
                )
            ).scalar_one()
        )


async def counts(engine: AsyncEngine, session_id: str) -> dict[str, int]:
    """The four counts `/me` publishes, read through the same function it uses.

    Not through `/me` itself: these fixtures' `@example.test` addresses make it
    a 500 (#321), which is a different bug.
    """
    async with engine.connect() as conn:
        mentor_id, mentee_id = (
            await conn.execute(
                text("SELECT mentor_id, mentee_id FROM sessions WHERE id = :i"), {"i": session_id}
            )
        ).one()
    async with async_sessionmaker(engine)() as session:
        now = dt.datetime.now(dt.UTC)
        as_mentor = await booking_counts(session, UUID(str(mentor_id)), now)
        as_mentee = await booking_counts(session, UUID(str(mentee_id)), now)
    return {
        "mentor_awaiting": as_mentor["mentor_awaiting"],
        "mentee_awaiting": as_mentee["mentee_awaiting"],
    }


async def test_an_undated_request_whose_session_started_awaits_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    request = await a_request(db_engine, api_client, "ud-started")
    await undated(db_engine, request["id"], started=True)

    got = await counts(db_engine, request["id"])

    assert got == {"mentor_awaiting": 0, "mentee_awaiting": 0}


async def test_an_undated_request_not_yet_started_still_awaits_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive half: no deadline and still ahead means still answerable."""
    request = await a_request(db_engine, api_client, "ud-ahead")
    await undated(db_engine, request["id"], started=False)

    got = await counts(db_engine, request["id"])

    assert got == {"mentor_awaiting": 1, "mentee_awaiting": 1}
    assert await sweep(db_engine) == 0


async def test_the_sweep_expires_an_undated_request_once_its_session_started(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    request = await a_request(db_engine, api_client, "ud-sweep")
    await undated(db_engine, request["id"], started=True)

    assert await sweep(db_engine) == 1
    assert await status_of(db_engine, request["id"]) == "expired"
    assert await sweep(db_engine) == 0


@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_a_started_undated_request_cannot_be_answered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, action: str
) -> None:
    request = await a_request(db_engine, api_client, f"ud-{action}")
    await undated(db_engine, request["id"], started=True)

    refused = await api_client.post(
        f"/api/v1/sessions/{request['id']}/{action}", headers=request["mentor"]
    )

    assert refused.status_code == 409, refused.text
    assert await status_of(db_engine, request["id"]) == "pending_mentor_approval"


async def test_expiring_a_request_that_never_spent_a_credit_refunds_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A migrated request has no `session_booked` debit, so the expiry refund
    has nothing to give back and must not mint a credit."""
    request = await a_request(db_engine, api_client, "ud-nospend")
    await undated(db_engine, request["id"], started=True)
    await without_spend(db_engine, request["id"])

    assert await sweep(db_engine) == 1
    assert await refund_lots(db_engine, request["id"]) == 0


async def test_expiring_a_paid_undated_request_refunds_its_credit(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The refund path is unchanged: a request that did spend gets it back once."""
    request = await a_request(db_engine, api_client, "ud-paid")
    await undated(db_engine, request["id"], started=True)

    assert await sweep(db_engine) == 1
    assert await refund_lots(db_engine, request["id"]) == 1

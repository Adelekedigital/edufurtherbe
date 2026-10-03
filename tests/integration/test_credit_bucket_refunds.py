"""A refunded credit goes back to the part of the card it came from (decision 232).

Through the real booking and decline, so the ledger link the split follows is
the one `spend_credit` and `refund_credit` actually write.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_bookable_offering, a_mentee
from tests.integration.test_api_credit_refunds import drain, mentor_token

from app.core.config import Settings
from app.domain.credits import credit_ladder
from app.infra.db.credit_store import CreditSummary, get_credit_summary
from app.infra.db.engine import create_session_factory

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LADDER = credit_ladder(Settings(_env_file=None))
SESSIONS = "/api/v1/sessions"
MONTH_END = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)


async def paid_from(
    engine: AsyncEngine, mentee: UUID, source: str, expires: dt.datetime | None
) -> None:
    """Make the mentee's only credit one of ``source``."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE credit_lots SET source = :s, expires_at = :e WHERE user_id = :u"),
            {"s": source, "e": expires, "u": mentee},
        )
    await drain(engine, mentee, leave=1)


async def book_then_decline(
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    tag: str,
    source: str,
    expires: dt.datetime | None,
) -> UUID:
    mentor, session_type = await a_bookable_offering(engine, tag)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = true WHERE user_id = :u"
            ),
            {"u": mentor},
        )
    mentee, token = await a_mentee(engine, tag)
    await paid_from(engine, mentee, source, expires)

    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots", params={"session_type_id": str(session_type)}
    )
    booked = await client.post(
        SESSIONS,
        json={"session_type_id": str(session_type), "starts_at": slots.json()["data"][-1]["start"]},
        headers=token | {"Idempotency-Key": str(uuid4())},
    )
    assert booked.status_code == 201, booked.text
    declined = await client.post(
        f"{SESSIONS}/{booked.json()['id']}/decline",
        json={},
        headers=await mentor_token(engine, mentor),
    )
    assert declined.status_code == 200, declined.text
    return mentee


async def summary(engine: AsyncEngine, user_id: UUID) -> CreditSummary:
    async with create_session_factory(engine)() as db:
        return await get_credit_summary(db, user_id, ladder=LADDER)


async def test_a_refunded_monthly_credit_comes_back_as_monthly(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    mentee = await book_then_decline(db_engine, api_client, "bk-monthly", "monthly_free", MONTH_END)

    result = await summary(db_engine, mentee)

    assert result.balance == 1
    assert (result.monthly.balance, result.bonus.balance) == (1, 0)


async def test_a_refunded_starter_credit_comes_back_as_bonus(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    mentee = await book_then_decline(db_engine, api_client, "bk-starter", "profile_completed", None)

    result = await summary(db_engine, mentee)

    assert result.balance == 1
    assert (result.monthly.balance, result.bonus.balance) == (0, 1)
    assert [(g.count, g.expires_at) for g in result.bonus.groups] == [(1, None)]

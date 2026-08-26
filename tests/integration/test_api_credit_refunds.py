"""A request that never became a session hands the credit back.

Booking debits. Before this, a mentor declining, a mentee withdrawing, or a
request expiring unanswered left the credit gone — and `credit_reason` had no
member that could even record its return. A mentee who booked and was declined
simply lost a credit, which reads as being charged for nothing rather than as a
missing feature. Found by code review of the consumption PR.

**The case that decides the schema is the double sweep.** The expiry job runs
hourly and calls itself idempotent; if the new reason sat outside the
one-refund-per-session index, every pass would pay the same request again.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_bookable_offering, a_mentee

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

SESSIONS = "/api/v1/sessions"


async def balance_of(engine: AsyncEngine, user_id: UUID) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT COALESCE(SUM(quantity_remaining), 0) "
                        "FROM credit_lots WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
        )


async def ledger_of(engine: AsyncEngine, user_id: UUID) -> list[Any]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT delta, reason FROM credit_transactions "
                "WHERE user_id = :u ORDER BY created_at, delta"
            ),
            {"u": user_id},
        )
        return [tuple(row) for row in rows]


async def drain(engine: AsyncEngine, user_id: UUID, *, leave: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE credit_lots SET quantity_remaining = :n WHERE user_id = :u"),
            {"n": leave, "u": user_id},
        )


async def a_request(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str
) -> tuple[UUID, UUID, dict[str, str], str]:
    """A booked request awaiting the mentor, on a mentee with one credit."""
    mentor, session_type = await a_bookable_offering(engine, tag)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = true WHERE user_id = :u"
            ),
            {"u": mentor},
        )
    mentee, token = await a_mentee(engine, tag)
    await drain(engine, mentee, leave=1)

    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    when = str(slots.json()["data"][0]["start"])
    booked = await client.post(
        SESSIONS,
        json={"session_type_id": str(session_type), "starts_at": when},
        headers=token | {"Idempotency-Key": str(uuid4())},
    )
    assert booked.status_code == 201, booked.text
    return mentee, mentor, token, booked.json()["id"]


async def mentor_token(engine: AsyncEngine, mentor: UUID) -> dict[str, str]:
    from conftest import api_token, bearer

    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
    return bearer(api_token(auth_id))


# --------------------------------------------------------------------------
# The three ways a request ends without a session
# --------------------------------------------------------------------------


async def test_a_declined_request_returns_the_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The case that reads as theft.** The mentee paid, the mentor said no,
    and nothing was delivered."""
    mentee, mentor, _, session_id = await a_request(db_engine, api_client, "rf-decline")
    assert await balance_of(db_engine, mentee) == 0

    declined = await api_client.post(
        f"{SESSIONS}/{session_id}/decline",
        json={},
        headers=await mentor_token(db_engine, mentor),
    )

    assert declined.status_code == 200, declined.text
    assert await balance_of(db_engine, mentee) == 1
    # The fixture's opening grant, the debit, the refund. Asserted whole rather
    # than filtered: every movement having a row is the invariant, and a
    # fixture that funded without one would hide exactly that.
    assert await ledger_of(db_engine, mentee) == [
        (20, "grant"),
        (-1, "session_booked"),
        (1, "request_unfulfilled"),
    ]


async def test_a_withdrawn_request_returns_the_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The mentee changed their mind before anybody committed anything."""
    mentee, _, token, session_id = await a_request(db_engine, api_client, "rf-withdraw")

    withdrawn = await api_client.post(f"{SESSIONS}/{session_id}/withdraw", json={}, headers=token)

    assert withdrawn.status_code == 200, withdrawn.text
    assert await balance_of(db_engine, mentee) == 1


async def test_an_expired_request_returns_the_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Nobody decided this — the mentor simply never answered. The credit is
    still not owed."""
    from app.infra.db.session_writer import expire_requests

    mentee, _, _, session_id = await a_request(db_engine, api_client, "rf-expire")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET respond_by = :t WHERE id = :i"),
            {"t": dt.datetime(2020, 1, 1, tzinfo=dt.UTC), "i": session_id},
        )

    from app.infra.db.engine import create_session_factory

    factory = create_session_factory(db_engine)
    async with factory() as db:
        await expire_requests(db, now=dt.datetime.now(dt.UTC))
        await db.commit()

    assert await balance_of(db_engine, mentee) == 1


async def test_a_second_sweep_refunds_once(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The case the widened index exists for.**

    The expiry sweep runs hourly and its own docstring calls it idempotent. Had
    the new reason sat outside `uq_credit_transactions_one_refund_per_session`,
    every pass would have paid the same request again — and the balance would
    have climbed by one an hour, forever.
    """
    from app.infra.db.engine import create_session_factory
    from app.infra.db.session_writer import expire_requests

    mentee, _, _, session_id = await a_request(db_engine, api_client, "rf-twice")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET respond_by = :t WHERE id = :i"),
            {"t": dt.datetime(2020, 1, 1, tzinfo=dt.UTC), "i": session_id},
        )

    factory = create_session_factory(db_engine)
    for _ in range(3):
        async with factory() as db:
            await expire_requests(db, now=dt.datetime.now(dt.UTC))
            await db.commit()

    assert await balance_of(db_engine, mentee) == 1
    # The fixture's opening grant, the debit, the refund. Asserted whole rather
    # than filtered: every movement having a row is the invariant, and a
    # fixture that funded without one would hide exactly that.
    assert await ledger_of(db_engine, mentee) == [
        (20, "grant"),
        (-1, "session_booked"),
        (1, "request_unfulfilled"),
    ]


async def test_the_refunded_credit_is_a_fresh_lot(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Decision 11: a refund creates a lot rather than returning to the original.

    The original may already be dead by the time a sweep settles, and returning
    a credit to a dead lot refunds nothing.
    """
    mentee, mentor, _, session_id = await a_request(db_engine, api_client, "rf-lot")

    await api_client.post(
        f"{SESSIONS}/{session_id}/decline",
        json={},
        headers=await mentor_token(db_engine, mentor),
    )

    async with db_engine.begin() as conn:
        sources = [
            row[0]
            for row in await conn.execute(
                text("SELECT source FROM credit_lots WHERE user_id = :u ORDER BY created_at"),
                {"u": mentee},
            )
        ]

    assert sources == ["opening_balance", "refund"]


async def test_the_refund_can_be_spent_again(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The accepting case that makes the rest worth anything: the credit is
    genuinely usable, not merely recorded."""
    mentee, mentor, token, session_id = await a_request(db_engine, api_client, "rf-respend")
    await api_client.post(
        f"{SESSIONS}/{session_id}/decline",
        json={},
        headers=await mentor_token(db_engine, mentor),
    )

    other_mentor, other_type = await a_bookable_offering(db_engine, "rf-respend-2")
    slots = await api_client.get(
        f"/api/v1/users/{other_mentor}/availability/slots",
        params={"session_type_id": str(other_type)},
    )
    rebooked = await api_client.post(
        SESSIONS,
        json={
            "session_type_id": str(other_type),
            "starts_at": str(slots.json()["data"][0]["start"]),
        },
        headers=token | {"Idempotency-Key": str(uuid4())},
    )

    assert rebooked.status_code == 201, rebooked.text
    assert await balance_of(db_engine, mentee) == 0

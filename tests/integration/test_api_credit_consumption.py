"""Booking spends a credit, and cannot spend one twice.

**The two tests that matter here are the refusal and the race.** Everything
else about consumption is arithmetic; those two are the ones where being wrong
costs somebody a free session or a lost credit.

The concurrency test runs two genuinely simultaneous bookings on separate
connections and asserts the **rows**. Asserting that a lock was called proves
nothing — `pg_advisory_xact_lock` does not error when it is taken on the wrong
key or outside the transaction, it simply fails to protect, and every test that
mocks it passes either way.
"""

from __future__ import annotations

import asyncio
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
                "SELECT delta, reason, session_id IS NOT NULL FROM credit_transactions "
                "WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user_id},
        )
        return [tuple(row) for row in rows]


async def drain(engine: AsyncEngine, user_id: UUID, *, leave: int = 0) -> None:
    """Spend a mentee down to ``leave`` credits, without going through booking."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE credit_lots SET quantity_remaining = :n WHERE user_id = :u"),
            {"n": leave, "u": user_id},
        )


async def book(
    client: httpx.AsyncClient, token: dict[str, str], session_type: UUID, when: str
) -> httpx.Response:
    return await client.post(
        SESSIONS,
        json={"session_type_id": str(session_type), "starts_at": when},
        headers=token | {"Idempotency-Key": str(uuid4())},
    )


async def a_slot(client: httpx.AsyncClient, mentor: UUID, session_type: UUID) -> str:
    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    return str(slots.json()["data"][0]["start"])


# --------------------------------------------------------------------------
# Spending
# --------------------------------------------------------------------------


async def test_booking_spends_one_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "cc-spend")
    mentee, token = await a_mentee(db_engine, "cc-spend")
    await drain(db_engine, mentee, leave=3)

    response = await book(
        api_client, token, session_type, await a_slot(api_client, mentor, session_type)
    )

    assert response.status_code == 201
    assert await balance_of(db_engine, mentee) == 2


async def test_the_debit_names_the_session_it_paid_for(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**D8's first reason the ledger exists.**

    *"I was charged for a session that never ran"* is only answerable if the
    charge names the session — which is why the debit is written after the
    insert rather than before it.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "cc-names")
    mentee, token = await a_mentee(db_engine, "cc-names")
    await drain(db_engine, mentee, leave=1)

    when = await a_slot(api_client, mentor, session_type)
    await book(api_client, token, session_type, when)

    # The opening grant the fixture writes, then the debit. Asserting both
    # rather than filtering: the invariant is that every movement has a row,
    # and a fixture that funded without one would hide exactly that.
    assert await ledger_of(db_engine, mentee) == [
        (20, "grant", False),
        (-1, "session_booked", True),
    ]


async def test_booking_at_zero_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The gate.** At zero the card's call to action is dead and the server
    refuses — the two have to agree, or the client shows a button that 500s."""
    mentor, session_type = await a_bookable_offering(db_engine, "cc-zero")
    mentee, token = await a_mentee(db_engine, "cc-zero")
    await drain(db_engine, mentee)

    response = await book(
        api_client, token, session_type, await a_slot(api_client, mentor, session_type)
    )

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/insufficient-credit"


async def test_a_refused_booking_creates_no_session(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The refusal and the insert are one transaction. A session left behind by
    a failed payment is a free booking nobody would ever notice."""
    mentor, session_type = await a_bookable_offering(db_engine, "cc-none")
    mentee, token = await a_mentee(db_engine, "cc-none")
    await drain(db_engine, mentee)

    when = await a_slot(api_client, mentor, session_type)
    await book(api_client, token, session_type, when)

    async with db_engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM sessions WHERE mentee_id = :u"), {"u": mentee}
            )
        ).scalar_one()

    assert count == 0


async def test_an_expired_lot_cannot_pay(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The spend filters `expires_at` itself, exactly as the read does. If it
    trusted the expiry job, a night the job did not run would let somebody book
    with a credit that died last month."""
    mentor, session_type = await a_bookable_offering(db_engine, "cc-expired")
    mentee, token = await a_mentee(db_engine, "cc-expired")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE credit_lots SET expires_at = :t WHERE user_id = :u"),
            {"t": dt.datetime(2020, 1, 1, tzinfo=dt.UTC), "u": mentee},
        )

    response = await book(
        api_client, token, session_type, await a_slot(api_client, mentor, session_type)
    )

    assert response.status_code == 409


async def test_the_soonest_expiring_lot_pays_first(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Burn perishable before permanent.**

    It minimises the balance that quietly dies at the reset, and once payments
    land it is the difference between spending a free credit and spending a
    purchased one — which is a refund conversation versus a chargeback.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "cc-order")
    mentee, token = await a_mentee(db_engine, "cc-order")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE credit_lots SET quantity_remaining = 1, expires_at = NULL "
                "WHERE user_id = :u"
            ),
            {"u": mentee},
        )
        await conn.execute(
            text(
                "INSERT INTO credit_lots "
                "(user_id, source, quantity_granted, quantity_remaining, expires_at) "
                "VALUES (:u, 'monthly_free', 1, 1, :t)"
            ),
            {"u": mentee, "t": dt.datetime(2099, 1, 1, tzinfo=dt.UTC)},
        )

    when = await a_slot(api_client, mentor, session_type)
    await book(api_client, token, session_type, when)

    async with db_engine.begin() as conn:
        spent = (
            await conn.execute(
                text(
                    "SELECT source FROM credit_lots WHERE user_id = :u AND quantity_remaining = 0"
                ),
                {"u": mentee},
            )
        ).scalar_one()

    assert spent == "monthly_free"


# --------------------------------------------------------------------------
# The race — the reason the advisory lock exists
# --------------------------------------------------------------------------


async def test_two_concurrent_bookings_cannot_spend_one_credit_twice(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Genuinely concurrent, and asserted on the rows.**

    A wrong lock is invisible: `pg_advisory_xact_lock` does not error when it is
    taken on the wrong key or outside the transaction — it simply fails to
    protect. So this fires two real requests at once against a balance of one
    and checks what the database ended up holding.

    Two different slots, because the mentor's exclusion constraint would refuse
    the second booking on its own and the test would pass without the lock
    doing anything. The only thing that can stop the second here is the credit.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "cc-race")
    mentee, token = await a_mentee(db_engine, "cc-race")
    await drain(db_engine, mentee, leave=1)

    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    first, second = (str(s["start"]) for s in slots.json()["data"][:2])

    responses = await asyncio.gather(
        book(api_client, token, session_type, first),
        book(api_client, token, session_type, second),
        return_exceptions=True,
    )

    codes = sorted(r.status_code for r in responses if isinstance(r, httpx.Response))

    assert codes == [201, 409], f"expected one booking and one refusal, got {codes}"
    assert await balance_of(db_engine, mentee) == 0
    # The grant, plus exactly one debit — the second booking wrote nothing.
    assert len(await ledger_of(db_engine, mentee)) == 2


async def test_the_balance_never_goes_negative(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The second wall, behind the lock.

    `quantity_remaining >= 0` is a database constraint, so even if the lock were
    wrong the worst outcome is a failed booking rather than a balance below
    zero — a negative balance is unrepresentable, not merely unlikely.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "cc-negative")
    mentee, token = await a_mentee(db_engine, "cc-negative")
    await drain(db_engine, mentee, leave=1)

    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    starts = [str(s["start"]) for s in slots.json()["data"][:3]]

    responses = await asyncio.gather(
        *(book(api_client, token, session_type, when) for when in starts),
        return_exceptions=True,
    )

    assert await balance_of(db_engine, mentee) >= 0

    # `balance >= 0` alone is satisfied by a 500: a CHECK violation aborts the
    # transaction, leaving the balance non-negative and the caller with nothing
    # actionable. So the contract is asserted too — no 5xx, exactly one booking.
    #
    # **This does not reliably discriminate, and saying so matters.** Three
    # requests do not race as dependably as two, and while the advisory lock
    # holds, the guarded decrement is unreachable by construction — the second
    # booking reads a zero balance and refuses before it. The test that does
    # discriminate is `test_two_concurrent_bookings_...`, which was watched
    # failing with the lock removed.
    codes = sorted(r.status_code for r in responses if isinstance(r, httpx.Response))
    assert all(code < 500 for code in codes), f"a lost race answered with a 5xx: {codes}"
    assert codes.count(201) == 1, f"exactly one booking should succeed, got {codes}"

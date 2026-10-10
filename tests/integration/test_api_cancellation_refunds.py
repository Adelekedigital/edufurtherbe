"""A confirmed session that does not happen: whose credit comes back (#335).

The owner's rule (decision 229): a mentor's cancellation always refunds the
mentee; a mentee's refunds only with twelve hours' notice; a mentor no-show
refunds the mentee who came. Before this, none of the three refunded anything,
so a mentor calling off a session cost the mentee a credit for nothing.

Every session is booked through the API and then moved in time, as the
attendance suite does: the product cannot produce a session twelve hours away
on a fixed clock, and moving the row is honest where moving the machine is not.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_attendance import a_confirmed_session, join_url, settle
from tests.integration.test_api_credit_refunds import balance_of
from tests.integration.test_api_reminder_callback import a_pending_request

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: What `conftest.fund` gives every test mentee.
FUNDED = 20


async def refunds_of(engine: AsyncEngine, session_id: str) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT reason FROM credit_transactions "
                "WHERE session_id = :i AND delta > 0 ORDER BY created_at"
            ),
            {"i": session_id},
        )
        return [str(row[0]) for row in rows]


async def cancel(
    client: httpx.AsyncClient, booking: dict[str, Any], by: str, **body: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/sessions/{booking['id']}/cancel", json=body, headers=booking[by]
    )


# --------------------------------------------------------------------------
# A mentor cancels: always refunded
# --------------------------------------------------------------------------


async def test_a_mentor_cancelling_refunds_the_mentee(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No reason given, an hour to go: the mentee still gets the credit back.
    The reason code decides nothing, because the design sends none."""
    booking = await a_confirmed_session(
        db_engine, api_client, "cr-mentor", starts_in=dt.timedelta(hours=1)
    )
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED - 1

    cancelled = await cancel(api_client, booking, "mentor")

    assert cancelled.status_code == 200, cancelled.text
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED
    assert await refunds_of(db_engine, booking["id"]) == ["session_cancelled_refund"]


async def test_a_mentor_cancelling_with_a_reason_refunds_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_confirmed_session(
        db_engine, api_client, "cr-mentor-code", starts_in=dt.timedelta(hours=1)
    )

    cancelled = await cancel(api_client, booking, "mentor", reason_code="scheduling_conflict")

    assert cancelled.status_code == 200, cancelled.text
    assert await refunds_of(db_engine, booking["id"]) == ["session_cancelled_refund"]


# --------------------------------------------------------------------------
# A mentee cancels: refunded with twelve hours' notice
# --------------------------------------------------------------------------


async def test_a_mentee_cancelling_early_gets_the_credit_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_confirmed_session(
        db_engine, api_client, "cr-mentee-early", starts_in=dt.timedelta(hours=13)
    )

    cancelled = await cancel(api_client, booking, "mentee")

    assert cancelled.status_code == 200, cancelled.text
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED
    assert await refunds_of(db_engine, booking["id"]) == ["session_cancelled_refund"]


async def test_a_mentee_cancelling_just_outside_twelve_hours_is_refunded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A minute of slack for the request's own travel; the exact instant is
    pinned in the unit suite, where the clock is a parameter."""
    booking = await a_confirmed_session(
        db_engine, api_client, "cr-mentee-edge", starts_in=dt.timedelta(hours=12, minutes=1)
    )

    await cancel(api_client, booking, "mentee")

    assert await refunds_of(db_engine, booking["id"]) == ["session_cancelled_refund"]


async def test_a_mentee_cancelling_late_uses_the_credit(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Inside twelve hours the cancellation goes through, and the credit stays
    spent."""
    booking = await a_confirmed_session(
        db_engine, api_client, "cr-mentee-late", starts_in=dt.timedelta(hours=11, minutes=59)
    )

    cancelled = await cancel(api_client, booking, "mentee")

    assert cancelled.status_code == 200, cancelled.text
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED - 1
    assert await refunds_of(db_engine, booking["id"]) == []


# --------------------------------------------------------------------------
# The attendance settlement: a mentor no-show refunds
# --------------------------------------------------------------------------


async def missed(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, *joins: str
) -> tuple[str, Any]:
    """A session the named parties joined, then pushed past its join window.
    Returns its id and the mentee's id."""
    booking = await a_confirmed_session(engine, client, tag, starts_in=dt.timedelta(minutes=1))
    for party in joins:
        joined = await client.post(join_url(booking), headers=booking[party])
        assert joined.status_code == 200, joined.text
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": booking["id"]},
        )
    return str(booking["id"]), booking["mentee_id"]


async def test_a_mentor_no_show_refunds_the_mentee(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_id, mentee = await missed(db_engine, api_client, "cr-ns-mentor", "mentee")
    assert await balance_of(db_engine, mentee) == FUNDED - 1

    await settle(db_engine)

    assert await refunds_of(db_engine, session_id) == ["session_no_show_refund"]
    assert await balance_of(db_engine, mentee) == FUNDED


async def test_a_second_settlement_refunds_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_id, mentee = await missed(db_engine, api_client, "cr-ns-twice", "mentee")

    await settle(db_engine)
    await settle(db_engine)

    assert await refunds_of(db_engine, session_id) == ["session_no_show_refund"]
    assert await balance_of(db_engine, mentee) == FUNDED


async def test_a_mentee_no_show_refunds_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_id, mentee = await missed(db_engine, api_client, "cr-ns-mentee", "mentor")

    await settle(db_engine)

    assert await refunds_of(db_engine, session_id) == []
    assert await balance_of(db_engine, mentee) == FUNDED - 1


async def test_a_session_both_missed_refunds_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_id, mentee = await missed(db_engine, api_client, "cr-ns-both")

    await settle(db_engine)

    assert await refunds_of(db_engine, session_id) == []
    assert await balance_of(db_engine, mentee) == FUNDED - 1


async def test_a_completed_session_refunds_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_id, mentee = await missed(db_engine, api_client, "cr-ns-done", "mentor", "mentee")

    await settle(db_engine)

    assert await refunds_of(db_engine, session_id) == []
    assert await balance_of(db_engine, mentee) == FUNDED - 1


# --------------------------------------------------------------------------
# Nothing spent, nothing refunded
# --------------------------------------------------------------------------


async def test_a_session_nobody_paid_for_mints_no_credit(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A migrated session never spent a credit in this ledger. Cancelling it
    must not create one out of nothing."""
    booking = await a_confirmed_session(
        db_engine, api_client, "cr-unpaid", starts_in=dt.timedelta(hours=1)
    )
    async with db_engine.begin() as conn:
        # Remove the debit: what a migrated row looks like, with the balance
        # left as it was. (A debit cannot be detached — the ledger's CHECK
        # requires a booking debit to name its session.)
        await conn.execute(
            text("DELETE FROM credit_transactions WHERE session_id = :i"),
            {"i": booking["id"]},
        )

    cancelled = await cancel(api_client, booking, "mentor")

    assert cancelled.status_code == 200, cancelled.text
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED - 1


# --------------------------------------------------------------------------
# The window is a setting, and published (owner, 2026-10-10)
# --------------------------------------------------------------------------


def with_window(client: httpx.AsyncClient, hours: int) -> None:
    """Run this app on a `MENTEE_CANCEL_REFUND_HOURS` of ``hours``."""
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(update={"mentee_cancel_refund_hours": hours})


@pytest.mark.parametrize(
    ("starts_in", "refunded"),
    [(dt.timedelta(hours=7), True), (dt.timedelta(hours=5), False)],
    ids=["outside-six", "inside-six"],
)
async def test_the_window_follows_the_setting(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    starts_in: dt.timedelta,
    refunded: bool,
) -> None:
    """**Six hours configured, six hours applied**, not the twelve that were a
    constant until the owner made it a deploy setting."""
    with_window(api_client, 6)
    booking = await a_confirmed_session(
        db_engine, api_client, f"cr-window-{refunded}".lower(), starts_in=starts_in
    )

    cancelled = await cancel(api_client, booking, "mentee")

    assert cancelled.status_code == 200, cancelled.text
    assert (await refunds_of(db_engine, booking["id"])) == (
        ["session_cancelled_refund"] if refunded else []
    )


@pytest.mark.parametrize("hours", [12, 6])
async def test_a_confirmed_session_publishes_its_refund_deadline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, hours: int
) -> None:
    """**The frontend reads the deadline rather than repeating the number**, so
    a changed setting cannot leave the copy stating the old one."""
    with_window(api_client, hours)
    booking = await a_confirmed_session(
        db_engine, api_client, f"cr-deadline-{hours}", starts_in=dt.timedelta(hours=20)
    )

    read = await api_client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])

    body = read.json()
    starts_at = dt.datetime.fromisoformat(body["starts_at"])
    assert dt.datetime.fromisoformat(body["refund_until"]) == starts_at - dt.timedelta(hours=hours)


async def test_me_publishes_the_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """For the credits explainer, which has no session to read a deadline from."""
    with_window(api_client, 6)
    booking = await a_confirmed_session(
        db_engine, api_client, "cr-me-window", starts_in=dt.timedelta(hours=20)
    )

    me = await api_client.get("/api/v1/me", headers=booking["mentee"])

    assert me.json()["mentee_cancel_refund_hours"] == 6


async def test_a_pending_request_publishes_no_refund_deadline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Only a confirmed session has one.** Withdrawing or declining a pending
    request always refunds, so a deadline there would be a false warning."""
    pending = await a_pending_request(db_engine, api_client, "cr-pending-deadline")

    read = await api_client.get(
        f"/api/v1/sessions/{pending['id']}", headers=pending["mentor_headers"]
    )

    assert read.json()["status"] == "pending_mentor_approval"
    assert read.json()["refund_until"] is None

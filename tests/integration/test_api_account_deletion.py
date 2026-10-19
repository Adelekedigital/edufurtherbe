"""`DELETE /me`: deleting your own account, with 30 days to restore it.

Mentee Settings PRD, "Backend: delete account". One transaction ends every open
session the person is part of, expires their credits, unlists a mentor profile,
drops offers nobody can take any more and stamps the deletion; Supabase signs
them out everywhere after it commits.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_session_type,
    make_public_mentor,
    token_for,
)
from tests.integration.test_api_session_transitions import a_booking, url

from app.infra.auth.admin import AdminApiError
from conftest import add_user, api_token, bearer, fund

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CONFIRM = {"confirm": "DELETE"}


class FakeAuthAdmin:
    """Records each global sign-out; ``fail`` makes Supabase refuse it."""

    def __init__(self, *, fail: bool = False) -> None:
        self.signed_out: list[str] = []
        self.fail = fail

    def sign_out_everywhere(self, access_token: str) -> None:
        if self.fail:
            raise AdminApiError("sign out failed with 500")
        self.signed_out.append(access_token)


def wire(client: httpx.AsyncClient, admin: FakeAuthAdmin) -> FakeAuthAdmin:
    client._transport.app.state.auth_admin = admin  # type: ignore[attr-defined]
    return admin


def delete_me(client: httpx.AsyncClient, headers: dict[str, str], body: Any = CONFIRM) -> Any:
    return client.request("DELETE", "/api/v1/me", json=body, headers=headers)


async def one(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).mappings().one()


async def a_mentee(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth = uuid4()
    async with engine.begin() as conn:
        user = await add_user(
            conn,
            f"mentee-{tag}@example.test",
            auth_id=auth,
            first_name="Ada",
            primary_role="mentee",
        )
        await fund(conn, user)
    return user, bearer(api_token(auth))


async def event_of(engine: AsyncEngine, session_id: str) -> dict[str, Any]:
    return dict(
        await one(
            engine,
            "SELECT s.status, e.reason_code, e.actor_id, e.actor_type FROM sessions s "
            "JOIN session_events e ON e.session_id = s.id AND e.to_status = s.status "
            "WHERE s.id = :i",
            i=session_id,
        )
    )


async def told(engine: AsyncEngine, user: UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT event_type FROM outbox_events "
                "WHERE payload ->> 'recipient_id' = :u ORDER BY created_at"
            ),
            {"u": str(user)},
        )
        return [str(row[0]) for row in rows]


async def refunds_to(engine: AsyncEngine, user: UUID) -> int:
    row = await one(
        engine,
        "SELECT count(*) AS n FROM credit_transactions "
        "WHERE user_id = :u AND reason IN ('session_cancelled_refund', 'request_unfulfilled')",
        u=user,
    )
    return int(row["n"])


# --------------------------------------------------------------------------
# The confirmation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [{"confirm": "delete"}, {"confirm": "DELETE "}, {}, None])
async def test_anything_but_the_word_is_refused_and_changes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: Any
) -> None:
    admin = wire(api_client, FakeAuthAdmin())
    user, headers = await a_mentee(db_engine, "confirm")

    response = await delete_me(api_client, headers, body)

    assert response.status_code == 422, response.text
    assert (await api_client.get("/api/v1/me", headers=headers)).status_code == 200
    row = await one(
        db_engine, "SELECT deleted_at, deletion_purge_at FROM users WHERE id = :u", u=user
    )
    assert row["deleted_at"] is None and row["deletion_purge_at"] is None
    assert admin.signed_out == []


async def test_a_deleted_account_is_gone_and_signed_out(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = wire(api_client, FakeAuthAdmin())
    user, headers = await a_mentee(db_engine, "gone")
    before = dt.datetime.now(dt.UTC)

    response = await delete_me(api_client, headers)

    assert response.status_code == 204, response.text
    assert response.content == b""
    # The same token now reaches no account, and signing in again cannot make one.
    assert (await api_client.get("/api/v1/me", headers=headers)).status_code == 404
    assert (await delete_me(api_client, headers)).status_code == 404
    row = await one(
        db_engine, "SELECT deleted_at, deletion_purge_at FROM users WHERE id = :u", u=user
    )
    assert row["deleted_at"] >= before
    assert row["deletion_purge_at"] - row["deleted_at"] == dt.timedelta(days=30)
    assert admin.signed_out == [headers["Authorization"].removeprefix("Bearer ")]


async def test_a_failed_sign_out_still_deletes(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deletion is committed before Supabase is asked, and a refresh token
    that survives reaches no account here: the request still succeeds, and the
    failure is logged without the token."""
    # Alembic's `fileConfig` disables every logger that existed when the schema
    # migrated; switch this one back on (the `logs` fixture's pattern).
    monkeypatch.setattr(logging.getLogger("app.api.deps.account"), "disabled", False)
    wire(api_client, FakeAuthAdmin(fail=True))
    _, headers = await a_mentee(db_engine, "signout-fails")
    token = headers["Authorization"].removeprefix("Bearer ")

    response = await delete_me(api_client, headers)

    assert response.status_code == 204, response.text
    assert (await api_client.get("/api/v1/me", headers=headers)).status_code == 404
    assert "sign-out" in caplog.text
    assert token not in caplog.text


# --------------------------------------------------------------------------
# Open sessions
# --------------------------------------------------------------------------


async def a_mentor(engine: AsyncEngine, tag: str, *, confirm: bool) -> tuple[UUID, UUID]:
    """A bookable mentor, booked straight into `confirmed` unless ``confirm``."""
    mentor = await make_public_mentor(engine, tag)
    session_type = await add_session_type(engine, mentor, duration=60, notice=0)
    for day in range(7):
        await add_availability(engine, mentor, day_of_week=day, start="00:00", end="23:00")
    await set_confirmation(engine, mentor, confirm=confirm)
    return mentor, session_type


async def set_confirmation(engine: AsyncEngine, mentor: UUID, *, confirm: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = :c WHERE user_id = :u"
            ),
            {"c": confirm, "u": mentor},
        )


async def book(
    client: httpx.AsyncClient,
    mentor: UUID,
    session_type: UUID,
    headers: dict[str, str],
    *,
    slot: int = -1,
) -> str:
    """Book through the endpoint, from the end of the window so the cutoff never
    bites; ``slot`` picks another hour so one person's two bookings cannot overlap."""
    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots", params={"session_type_id": str(session_type)}
    )
    created = await client.post(
        "/api/v1/sessions",
        json={
            "session_type_id": str(session_type),
            "starts_at": slots.json()["data"][slot]["start"],
        },
        headers=headers | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_a_mentors_open_sessions_end_and_each_mentee_is_told_and_refunded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    wire(api_client, FakeAuthAdmin())
    mentor, session_type = await a_mentor(db_engine, "del-mentor", confirm=False)
    first, first_headers = await a_mentee(db_engine, "del-mentor-1")
    second, second_headers = await a_mentee(db_engine, "del-mentor-2")
    confirmed = await book(api_client, mentor, session_type, first_headers)
    await set_confirmation(db_engine, mentor, confirm=True)
    pending = await book(api_client, mentor, session_type, second_headers)

    response = await delete_me(api_client, bearer(await token_for(db_engine, mentor)))

    assert response.status_code == 204, response.text
    for session_id, mentee, status in (
        (confirmed, first, "cancelled"),
        (pending, second, "declined"),
    ):
        event = await event_of(db_engine, session_id)
        assert event == {
            "status": status,
            "reason_code": "account_deleted",
            "actor_id": mentor,
            "actor_type": "user",
        }
        assert await refunds_to(db_engine, mentee) == 1
    assert "session_cancelled" in await told(db_engine, first)
    assert "request_declined" in await told(db_engine, second)
    # Unlisted, with the reason on the event, and gone from public view.
    listing = await one(
        db_engine,
        "SELECT p.listing_status, e.reason FROM mentor_profiles p "
        "JOIN mentor_status_events e ON e.mentor_user_id = p.user_id "
        "WHERE p.user_id = :u ORDER BY e.created_at DESC LIMIT 1",
        u=mentor,
    )
    assert dict(listing) == {"listing_status": "unlisted", "reason": "account_deleted"}
    assert (await api_client.get(f"/api/v1/mentors/{mentor}")).status_code == 404


async def test_a_mentees_open_sessions_end_and_each_mentor_is_told(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    wire(api_client, FakeAuthAdmin())
    mentee, headers = await a_mentee(db_engine, "del-mentee")
    instant, instant_type = await a_mentor(db_engine, "del-mentee-c", confirm=False)
    asks, asks_type = await a_mentor(db_engine, "del-mentee-p", confirm=True)
    confirmed = await book(api_client, instant, instant_type, headers)
    pending = await book(api_client, asks, asks_type, headers, slot=-3)

    response = await delete_me(api_client, headers)

    assert response.status_code == 204, response.text
    for session_id, status in ((confirmed, "cancelled"), (pending, "withdrawn")):
        event = await event_of(db_engine, session_id)
        assert (event["status"], event["reason_code"]) == (status, "account_deleted")
        assert event["actor_id"] == mentee
    assert "session_cancelled" in await told(db_engine, instant)
    assert "request_withdrawn" in await told(db_engine, asks)


async def test_a_session_already_under_way_is_left_to_the_attendance_sweep(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    wire(api_client, FakeAuthAdmin())
    started = await a_booking(db_engine, api_client, "del-started", confirmed=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '5 minutes' WHERE id = :s"),
            {"s": started["id"]},
        )

    assert (await delete_me(api_client, started["mentee"])).status_code == 204

    row = await one(db_engine, "SELECT status FROM sessions WHERE id = :s", s=started["id"])
    assert row["status"] == "confirmed"


async def test_a_session_inside_the_cancellation_cutoff_still_ends(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The cutoff stops a party changing their mind at the last minute; a
    deleted account will not turn up, and the other person is better told."""
    wire(api_client, FakeAuthAdmin())
    soon = await a_booking(db_engine, api_client, "del-soon", confirmed=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() + interval '5 minutes' WHERE id = :s"),
            {"s": soon["id"]},
        )

    assert (await delete_me(api_client, soon["mentor"])).status_code == 204

    assert (await event_of(db_engine, soon["id"]))["status"] == "cancelled"


async def test_the_other_partys_sessions_with_somebody_else_are_untouched(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    wire(api_client, FakeAuthAdmin())
    mine = await a_booking(db_engine, api_client, "del-mine", confirmed=True)
    theirs = await a_booking(db_engine, api_client, "del-theirs", confirmed=True)

    assert (await delete_me(api_client, mine["mentee"])).status_code == 204

    row = await one(db_engine, "SELECT status FROM sessions WHERE id = :s", s=theirs["id"])
    assert row["status"] == "confirmed"


# --------------------------------------------------------------------------
# Credits and offers
# --------------------------------------------------------------------------


async def test_unused_credits_expire(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    wire(api_client, FakeAuthAdmin())
    user, headers = await a_mentee(db_engine, "credits")
    other, _ = await a_mentee(db_engine, "credits-other")

    assert (await delete_me(api_client, headers)).status_code == 204

    mine = await one(
        db_engine,
        "SELECT coalesce(sum(quantity_remaining), 0) AS left, "
        "(SELECT count(*) FROM credit_transactions t WHERE t.user_id = :u "
        " AND t.reason = 'lot_expired') AS expired FROM credit_lots WHERE user_id = :u",
        u=user,
    )
    assert mine["left"] == 0 and mine["expired"] >= 1
    theirs = await one(
        db_engine,
        "SELECT sum(quantity_remaining) AS left FROM credit_lots WHERE user_id = :u",
        u=other,
    )
    assert theirs["left"] > 0


async def test_an_offer_from_a_deleted_mentor_is_withdrawn(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An offered time nobody can book any more would still nudge the mentee."""
    wire(api_client, FakeAuthAdmin())
    pending = await a_booking(db_engine, api_client, "del-offer")
    later = dt.datetime.fromisoformat(pending["starts_at"]) - dt.timedelta(hours=2)
    declined = await api_client.post(
        url(pending, "decline"),
        json={"suggested_starts_at": later.isoformat()},
        headers=pending["mentor"],
    )
    assert declined.status_code == 200, declined.text
    offers = "SELECT count(*) AS n FROM session_suggestions WHERE mentor_id = :m"
    assert (await one(db_engine, offers, m=pending["mentor_id"]))["n"] == 1

    assert (await delete_me(api_client, pending["mentor"])).status_code == 204

    assert (await one(db_engine, offers, m=pending["mentor_id"]))["n"] == 0


# --------------------------------------------------------------------------
# A booking racing the deletion
# --------------------------------------------------------------------------


async def test_a_booking_racing_the_mentors_deletion_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The booking reads the mentor as live before the deletion commits; it must
    re-check under a lock rather than land a session on a deleted mentor."""
    mentor = await make_public_mentor(db_engine, "race")
    session_type = await add_session_type(db_engine, mentor, duration=60, notice=0)
    for day in range(7):
        await add_availability(db_engine, mentor, day_of_week=day, start="00:00", end="23:00")
    _, headers = await a_mentee(db_engine, "race")
    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots", params={"session_type_id": str(session_type)}
    )
    starts_at = str(slots.json()["data"][-1]["start"])

    async with db_engine.connect() as deleting:
        transaction = await deleting.begin()
        await deleting.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :m"), {"m": mentor}
        )
        booking = asyncio.create_task(
            api_client.post(
                "/api/v1/sessions",
                json={"session_type_id": str(session_type), "starts_at": starts_at},
                headers=headers | {"Idempotency-Key": str(uuid4())},
            )
        )
        await asyncio.sleep(1.0)
        assert not booking.done()
        await transaction.commit()
        response = await booking

    assert response.status_code == 404, response.text
    count = await one(
        db_engine, "SELECT count(*) AS n FROM sessions WHERE mentor_id = :m", m=mentor
    )
    assert count["n"] == 0

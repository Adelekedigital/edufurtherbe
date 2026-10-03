"""A mentor declines or cancels and suggests another time (#339, decision 230).

The owner's rules, each with a case: the original ends as it would have and is
refunded the same way; one time is offered, held for that mentee for two hours —
hidden from everyone else's grid and refused to anyone else's booking; the
mentee books it with an ordinary booking; one email tells them, and a reminder
goes thirty minutes before the hold lapses, unless they booked.

Every session is made through the API, as the transitions suite does, so no
fixture describes a state the product could not produce.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import until_blocked
from tests.integration.test_api_booking import a_mentee
from tests.integration.test_api_credit_refunds import balance_of
from tests.integration.test_api_session_reminders import wire
from tests.integration.test_api_session_transitions import a_booking

from app.core.errors import ConflictError
from app.domain.suggestions import SUGGESTION_REMINDER_KIND, SUGGESTION_REMINDER_LEAD
from conftest import PLATFORM_WINDOW

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: What `conftest.fund` gives every test mentee.
FUNDED = 20


async def offering_of(client: httpx.AsyncClient, booking: dict[str, Any]) -> str:
    read = await client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    return str(read.json()["session_type_id"])


async def open_slots(client: httpx.AsyncClient, booking: dict[str, Any]) -> list[str]:
    response = await client.get(
        f"/api/v1/users/{booking['mentor_id']}/availability/slots",
        params={"session_type_id": await offering_of(client, booking)},
    )
    return [str(slot["start"]) for slot in response.json()["data"]]


async def another_slot(client: httpx.AsyncClient, booking: dict[str, Any]) -> str:
    """A free time other than the booked one, away from the cancel cutoff."""
    return next(s for s in reversed(await open_slots(client, booking)) if s != booking["starts_at"])


async def end_with_suggestion(
    client: httpx.AsyncClient, booking: dict[str, Any], action: str, at: str, *, by: str = "mentor"
) -> httpx.Response:
    return await client.post(
        f"/api/v1/sessions/{booking['id']}/{action}",
        json={"suggested_starts_at": at, "reason_text": "Clash that day"},
        headers=booking[by],
    )


async def suggestion_of(client: httpx.AsyncClient, booking: dict[str, Any]) -> Any:
    read = await client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    return read.json()["suggestion"]


async def outbox_types(engine: AsyncEngine, session_id: str) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT event_type FROM outbox_events WHERE entity_id = :i "
                "AND event_type NOT IN ('session_requested', 'session_booked') ORDER BY created_at"
            ),
            {"i": session_id},
        )
        return [str(row[0]) for row in rows]


async def book(
    client: httpx.AsyncClient, session_type: str, at: str, headers: dict[str, str]
) -> httpx.Response:
    return await client.post(
        "/api/v1/sessions",
        json={"session_type_id": session_type, "starts_at": at},
        headers=headers | {"Idempotency-Key": str(uuid4())},
    )


async def lapse(engine: AsyncEngine, booking: dict[str, Any]) -> None:
    """Move the hold into the past, as the attendance suite moves sessions."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_suggestions SET held_until = now() - interval '1 minute' "
                "WHERE session_id = :i"
            ),
            {"i": booking["id"]},
        )


# --------------------------------------------------------------------------
# Suggesting
# --------------------------------------------------------------------------


async def test_declining_with_a_suggestion_ends_the_request_and_offers_the_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-decline")
    at = await another_slot(api_client, booking)

    declined = await end_with_suggestion(api_client, booking, "decline", at)

    assert declined.status_code == 200, declined.text
    read = await api_client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    assert read.json()["status"] == "declined"
    suggestion = read.json()["suggestion"]
    assert suggestion["status"] == "active"
    assert dt.datetime.fromisoformat(suggestion["starts_at"]) == dt.datetime.fromisoformat(at)
    assert suggestion["booked_session_id"] is None
    # The decline still refunds: the offer is separate from the ending.
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED
    # **One email, not two**: the suggestion replaces the decline message.
    assert await outbox_types(db_engine, booking["id"]) == ["session_time_suggested"]


async def test_a_mentor_cancelling_with_a_suggestion_ends_and_offers_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-cancel", confirmed=True)
    at = await another_slot(api_client, booking)

    cancelled = await end_with_suggestion(api_client, booking, "cancel", at)

    assert cancelled.status_code == 200, cancelled.text
    assert (await suggestion_of(api_client, booking))["status"] == "active"
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED
    assert await outbox_types(db_engine, booking["id"]) == ["session_time_suggested"]


async def test_a_plain_decline_still_sends_the_decline_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive case for `notify`: no suggestion, the usual message."""
    booking = await a_booking(db_engine, api_client, "sg-plain")

    await api_client.post(f"/api/v1/sessions/{booking['id']}/decline", headers=booking["mentor"])

    assert await outbox_types(db_engine, booking["id"]) == ["request_declined"]
    assert await suggestion_of(api_client, booking) is None


async def test_a_mentee_cannot_suggest_and_the_cancellation_does_not_land(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-mentee", confirmed=True)
    at = await another_slot(api_client, booking)

    refused = await end_with_suggestion(api_client, booking, "cancel", at, by="mentee")

    assert refused.status_code == 422, refused.text
    assert refused.json()["errors"][0]["pointer"] == "/suggested_starts_at"
    read = await api_client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    assert read.json()["status"] == "confirmed"


async def test_a_time_the_grid_does_not_offer_is_refused_and_nothing_lands(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-offgrid")
    off_grid = (
        dt.datetime.fromisoformat(await another_slot(api_client, booking))
        + dt.timedelta(minutes=17)
    ).isoformat()

    refused = await end_with_suggestion(api_client, booking, "decline", off_grid)

    assert refused.status_code == 422, refused.text
    assert refused.json()["errors"][0]["pointer"] == "/suggested_starts_at"
    read = await api_client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    assert read.json()["status"] == "pending_mentor_approval"
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED - 1


# --------------------------------------------------------------------------
# The hold
# --------------------------------------------------------------------------


async def test_the_held_time_leaves_the_public_grid(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-grid")
    at = await another_slot(api_client, booking)
    assert at in await open_slots(api_client, booking)

    await end_with_suggestion(api_client, booking, "decline", at)

    assert at not in await open_slots(api_client, booking)


async def test_another_mentee_cannot_book_the_held_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-other")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    _, stranger = await a_mentee(db_engine, "sg-other-b")

    refused = await book(api_client, session_type, at, stranger)

    # Sequentially the grid already hides it: a 422, as for any taken hour.
    assert refused.status_code == 422, refused.text


async def test_the_mentee_books_the_suggested_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-book")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    suggestion = await suggestion_of(api_client, booking)
    assert suggestion["status"] == "booked"
    assert suggestion["booked_session_id"] == booked.json()["id"]
    # Refunded by the decline, spent again by the new booking.
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED - 1


async def test_the_suggested_time_cannot_be_booked_twice(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-twice")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await book(api_client, session_type, at, booking["mentee"])

    again = await book(api_client, session_type, at, booking["mentee"])

    assert again.status_code == 422, again.text
    assert await balance_of(db_engine, booking["mentee_id"]) == FUNDED - 1


async def test_a_lapsed_hold_releases_the_time_to_everyone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-lapse")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)

    await lapse(db_engine, booking)

    assert at in await open_slots(api_client, booking)
    assert (await suggestion_of(api_client, booking))["status"] == "expired"
    _, stranger = await a_mentee(db_engine, "sg-lapse-b")
    assert (await book(api_client, session_type, at, stranger)).status_code == 201
    # The stranger's booking does not spend the mentee's lapsed offer.
    assert (await suggestion_of(api_client, booking))["status"] == "expired"


async def test_a_booking_racing_a_suggestion_is_refused_at_the_lock(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The `409`, the one way it is reachable.** The stranger reads the grid
    before the hold commits, so only the lock and the re-read under it stop the
    booking — the exclusion constraint cannot see a hold."""
    from app.infra.db.session_writer import book_session, suggest_time, transition

    booking = await a_booking(db_engine, api_client, "sg-race")
    session_type = await offering_of(api_client, booking)
    at = dt.datetime.fromisoformat(await another_slot(api_client, booking))
    stranger, _ = await a_mentee(db_engine, "sg-race-b")
    now = dt.datetime.now(dt.UTC)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    mentor = UUID(str(booking["mentor_id"]))

    async with factory() as first, factory() as second:
        await transition(first, UUID(booking["id"]), mentor, "decline", {}, now=now, notify=False)
        await suggest_time(
            first,
            UUID(booking["id"]),
            mentor,
            at,
            ended_as="declined",
            reason_text=None,
            now=now,
            window=PLATFORM_WINDOW,
        )
        racing = asyncio.create_task(
            book_session(
                second,
                stranger,
                {"session_type_id": UUID(session_type), "starts_at": at},
                now=now,
                require_answers=True,
                window=PLATFORM_WINDOW,
            )
        )
        await until_blocked(db_engine)
        await first.commit()
        with pytest.raises(ConflictError):
            await racing


# --------------------------------------------------------------------------
# The reminder
# --------------------------------------------------------------------------


async def test_the_reminder_is_scheduled_thirty_minutes_before_the_hold_lapses(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-schedule")
    at = await another_slot(api_client, booking)
    publisher = wire(api_client)

    await end_with_suggestion(api_client, booking, "decline", at)

    sent = [p for p in publisher.published if p["body"]["kind"] == SUGGESTION_REMINDER_KIND]
    assert len(sent) == 1
    held = dt.datetime.fromisoformat((await suggestion_of(api_client, booking))["held_until"])
    assert sent[0]["at"] == held - SUGGESTION_REMINDER_LEAD
    assert sent[0]["body"]["session_id"] == booking["id"]


async def reminders(engine: AsyncEngine, session_id: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox_events WHERE entity_id = :i "
                        "AND event_type = 'session_suggestion_reminder'"
                    ),
                    {"i": session_id},
                )
            ).scalar_one()
        )


async def fire(engine: AsyncEngine, session_id: str) -> bool:
    from app.infra.db.session_writer import remind_suggestion

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        queued = await remind_suggestion(session, UUID(session_id), SUGGESTION_REMINDER_KIND)
        await session.commit()
    return queued


async def test_the_reminder_goes_once_while_the_offer_is_open(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-remind")
    await end_with_suggestion(
        api_client, booking, "decline", await another_slot(api_client, booking)
    )

    assert await fire(db_engine, booking["id"]) is True
    await fire(db_engine, booking["id"])

    assert await reminders(db_engine, booking["id"]) == 1


async def test_no_reminder_once_the_offer_is_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-remind-booked")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await book(api_client, session_type, at, booking["mentee"])

    assert await fire(db_engine, booking["id"]) is False
    assert await reminders(db_engine, booking["id"]) == 0


async def test_no_reminder_once_the_hold_lapsed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-remind-lapsed")
    await end_with_suggestion(
        api_client, booking, "decline", await another_slot(api_client, booking)
    )
    await lapse(db_engine, booking)

    assert await fire(db_engine, booking["id"]) is False


async def test_only_the_mentee_it_was_offered_to_takes_up_the_offer(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """At the store, because the API never lets a stranger reach a held time:
    the scope is the last wall, so it is tested on its own."""
    from app.infra.db.session_writer.suggestions import attach_suggestion

    booking = await a_booking(db_engine, api_client, "sg-scope")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    stranger, _ = await a_mentee(db_engine, "sg-scope-b")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        taken = await attach_suggestion(
            session,
            mentor_id=UUID(str(booking["mentor_id"])),
            mentee_id=stranger,
            session_type_id=UUID(session_type),
            starts_at=dt.datetime.fromisoformat(at),
            booked_session_id=UUID(booking["id"]),
            now=dt.datetime.now(dt.UTC),
        )
        await session.rollback()

    assert taken is None
    assert (await suggestion_of(api_client, booking))["status"] == "active"

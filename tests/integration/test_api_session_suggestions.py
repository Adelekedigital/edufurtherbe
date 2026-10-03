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
    from app.infra.db.holds import held_offer

    booking = await a_booking(db_engine, api_client, "sg-scope")
    session_type = await offering_of(api_client, booking)
    at = dt.datetime.fromisoformat(await another_slot(api_client, booking))
    await end_with_suggestion(api_client, booking, "decline", at.isoformat())
    stranger, _ = await a_mentee(db_engine, "sg-scope-b")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    asked = {
        "mentor_id": UUID(str(booking["mentor_id"])),
        "session_type_id": UUID(session_type),
        "starts_at": at,
        "now": dt.datetime.now(dt.UTC),
    }

    async with factory() as session:
        theirs = await held_offer(session, mentee_id=UUID(str(booking["mentee_id"])), **asked)
        strangers = await held_offer(session, mentee_id=stranger, **asked)
        await session.rollback()

    assert theirs is not None
    assert strangers is None


# --------------------------------------------------------------------------
# Codex on #341
# --------------------------------------------------------------------------


async def test_a_queued_reminder_is_dropped_once_the_offer_is_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The drain runs on its own schedule, so the check is repeated there."""
    from app.infra.db.holds import suggestion_reminder_state

    booking = await a_booking(db_engine, api_client, "sg-drain")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def state() -> str:
        async with factory() as session:
            return await suggestion_reminder_state(
                session, UUID(booking["id"]), {}, dt.datetime.now(dt.UTC)
            )

    assert await state() == "due"
    await book(api_client, session_type, at, booking["mentee"])
    assert await state() == "stale"


async def test_a_lapsed_offer_is_stale_at_the_drain(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    from app.infra.db.holds import suggestion_reminder_state

    booking = await a_booking(db_engine, api_client, "sg-drain-lapse")
    await end_with_suggestion(
        api_client, booking, "decline", await another_slot(api_client, booking)
    )
    await lapse(db_engine, booking)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        found = await suggestion_reminder_state(
            session, UUID(booking["id"]), {}, dt.datetime.now(dt.UTC)
        )
    assert found == "stale"


async def test_the_holder_cannot_book_across_their_own_offer_at_another_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only the exact offer is theirs: half an hour later at another offering
    would book the mentor across the held hour and strand the offer."""
    from tests.integration.factories import add_session_type

    booking = await a_booking(db_engine, api_client, "sg-overlap")
    at = dt.datetime.fromisoformat(await another_slot(api_client, booking))
    await end_with_suggestion(api_client, booking, "decline", at.isoformat())
    short = await add_session_type(
        db_engine, UUID(str(booking["mentor_id"])), name="Quick", duration=30, notice=0
    )

    across = await book(
        api_client, str(short), (at + dt.timedelta(minutes=30)).isoformat(), booking["mentee"]
    )

    assert across.status_code == 422, across.text
    assert (await suggestion_of(api_client, booking))["status"] == "active"


async def test_the_offer_is_booked_at_the_length_it_was_offered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor shortening the offering inside the hold does not change what
    the offer becomes."""
    booking = await a_booking(db_engine, api_client, "sg-length")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET duration_minutes = 30 "
                "WHERE session_type_id = :t"
            ),
            {"t": session_type},
        )

    booked = await book(api_client, session_type, at, booking["mentee"])

    assert booked.status_code == 201, booked.text
    assert booked.json()["duration_minutes"] == 60
    assert (await suggestion_of(api_client, booking))["status"] == "booked"


async def test_the_locked_recheck_counts_the_offerings_break(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The hour straight after a held hour is still inside its break, as the
    grid would say; the hour after the break is not."""
    from app.infra.db.holds import holds_against

    booking = await a_booking(db_engine, api_client, "sg-break")
    session_type = await offering_of(api_client, booking)
    at = dt.datetime.fromisoformat(await another_slot(api_client, booking))
    await end_with_suggestion(api_client, booking, "decline", at.isoformat())
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET break_after_minutes = 30 "
                "WHERE session_type_id = :t"
            ),
            {"t": session_type},
        )
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    mentor = UUID(str(booking["mentor_id"]))
    now = dt.datetime.now(dt.UTC)

    async with factory() as session:
        next_hour = await holds_against(
            session,
            mentor,
            starts_at=at + dt.timedelta(hours=1),
            duration_minutes=60,
            break_minutes=0,
            now=now,
            except_id=None,
        )
        after_break = await holds_against(
            session,
            mentor,
            starts_at=at + dt.timedelta(minutes=90),
            duration_minutes=60,
            break_minutes=0,
            now=now,
            except_id=None,
        )

    assert next_hour is True
    assert after_break is False


class Recorder:
    """A notifier that sends nothing and remembers what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, **kwargs: Any) -> None:
        self.sent.append(str(kwargs["notification"]))


async def drained(engine: AsyncEngine, migrated_database: str) -> list[str]:
    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.config import Settings
    from app.infra.db.outbox import drain

    notifier = Recorder()
    async with AsyncSession(engine) as session:
        await drain(
            session,
            notifier=notifier,
            now=dt.datetime.now(dt.UTC),
            settings=Settings(_env_file=None, database_url=SecretStr(migrated_database)),
        )
        await session.commit()
    return notifier.sent


async def test_the_drain_sends_a_reminder_for_an_open_offer(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    booking = await a_booking(db_engine, api_client, "sg-drain-open")
    await end_with_suggestion(
        api_client, booking, "decline", await another_slot(api_client, booking)
    )
    await fire(db_engine, booking["id"])

    assert "session_suggestion_reminder" in await drained(db_engine, migrated_database)


async def test_the_drain_drops_a_reminder_for_an_offer_booked_since(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Queued at the callback, booked before the drain ran: not sent."""
    booking = await a_booking(db_engine, api_client, "sg-drain-booked")
    session_type = await offering_of(api_client, booking)
    at = await another_slot(api_client, booking)
    await end_with_suggestion(api_client, booking, "decline", at)
    await fire(db_engine, booking["id"])
    await book(api_client, session_type, at, booking["mentee"])

    assert "session_suggestion_reminder" not in await drained(db_engine, migrated_database)

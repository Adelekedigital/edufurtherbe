"""A mentee moves their session to another time in one call (decision 300).

The old session ends and the new one is booked in one transaction, so a refusal
leaves the old session exactly as it was. The credit that paid for the old
session pays for the new one: no refund and no second charge.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import token_for
from tests.integration.meeting_fakes import FakeCalendar, FakeRooms
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, body, key
from tests.integration.test_api_credit_refunds import balance_of
from tests.integration.test_api_meeting_provisioning import (  # noqa: F401 - fakes is a fixture
    a_mentor_on,
    fakes,
    venue_of,
)
from tests.integration.test_api_meeting_provisioning import book as book_on
from tests.integration.test_booking_answers import add_question

from app.core.config import Settings
from app.domain.credits import credit_ladder
from app.infra.db.credit_store import get_credit_summary
from app.infra.db.engine import create_session_factory
from conftest import A_REASON, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: What `conftest.fund` gives every test mentee.
FUNDED = 20

#: The room `FakeRooms` hands back.
FAKE_ROOM = "https://ef.daily.co/room"


async def later_slots(client: httpx.AsyncClient, mentor: UUID, session_type: UUID) -> list[str]:
    """Offered starts more than a day away, so neither the cancellation cutoff
    nor the refund window is anywhere near."""
    response = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    assert response.status_code == 200, response.text
    floor = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    starts = [
        str(s["start"])
        for s in response.json()["data"]
        if dt.datetime.fromisoformat(s["start"]) >= floor
    ]
    assert len(starts) >= 3, "the fixture mentor offers too little to move between"
    return starts


async def a_booking(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tag: str,
    *,
    mentor_confirms: bool = False,
    answers: list[dict[str, Any]] | None = None,
    question: bool = False,
) -> dict[str, Any]:
    """A session a mentee booked through the API, and what moving it needs."""
    mentor, session_type = await a_bookable_offering(engine, tag, mentor_confirms=mentor_confirms)
    question_id = None
    if question:
        question_id, _ = await add_question(engine, session_type, "What do you need?")
        answers = [{"question_id": str(question_id), "text": "Help with my essay"}]
    mentee, headers = await a_mentee(engine, tag)
    starts = await later_slots(client, mentor, session_type)
    payload = body(session_type, starts[0])
    if answers is not None:
        payload["answers"] = answers
    booked = await client.post("/api/v1/sessions", json=payload, headers=headers | key())
    assert booked.status_code == 201, booked.text
    return {
        "id": booked.json()["id"],
        "status": booked.json()["status"],
        "mentor": mentor,
        "mentee": mentee,
        "session_type": session_type,
        "headers": headers,
        "starts": starts,
        "question": question_id,
    }


async def move(
    client: httpx.AsyncClient,
    booking: dict[str, Any],
    starts_at: str,
    *,
    headers: dict[str, str] | None = None,
    idempotency: dict[str, str] | None = None,
    **extra: Any,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/sessions/{booking['id']}/move",
        json={"starts_at": starts_at, **extra},
        headers=(headers or booking["headers"]) | (idempotency or key()),
    )


async def session_row(engine: AsyncEngine, session_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, starts_at, rescheduled_from_session_id, session_type_id "
                    "FROM sessions WHERE id = :i"
                ),
                {"i": session_id},
            )
        ).one()
        return dict(row._mapping)


async def ledger(engine: AsyncEngine, mentee: UUID) -> list[tuple[int, str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT delta, reason, session_id FROM credit_transactions "
                "WHERE user_id = :u AND session_id IS NOT NULL ORDER BY created_at"
            ),
            {"u": mentee},
        )
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


async def last_event(engine: AsyncEngine, session_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT from_status, to_status, reason_code FROM session_events "
                    "WHERE session_id = :i ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"i": session_id},
            )
        ).one()
        return dict(row._mapping)


async def messages_about(engine: AsyncEngine, *session_ids: str) -> list[str]:
    """Which messages were queued about these sessions, one name per kind."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT DISTINCT event_type FROM outbox_events "
                "WHERE entity_id = ANY(CAST(:ids AS uuid[])) ORDER BY event_type"
            ),
            {"ids": list(session_ids)},
        )
        return [str(r[0]) for r in rows]


# --------------------------------------------------------------------------
# The move
# --------------------------------------------------------------------------


async def test_a_confirmed_session_moves_to_the_new_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-confirmed")

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    new = moved.json()
    assert new["id"] != booking["id"]
    assert new["status"] == "confirmed"
    assert dt.datetime.fromisoformat(new["starts_at"]) == dt.datetime.fromisoformat(
        booking["starts"][2]
    )
    old = await session_row(db_engine, booking["id"])
    assert old["status"] == "cancelled"
    assert await last_event(db_engine, booking["id"]) == {
        "from_status": "confirmed",
        "to_status": "cancelled",
        "reason_code": "rescheduled",
    }
    assert (
        str((await session_row(db_engine, new["id"]))["rescheduled_from_session_id"])
        == (booking["id"])
    )


async def test_the_credit_moves_with_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No refund and no second charge: the one debit pays for the new session."""
    booking = await a_booking(api_client, db_engine, "mv-credit")

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    assert await balance_of(db_engine, booking["mentee"]) == FUNDED - 1
    assert await ledger(db_engine, booking["mentee"]) == [(-1, "session_booked", booking["id"])]


async def test_a_moved_session_cancelled_early_refunds_its_credit_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The refund follows the move back to the debit that paid for it."""
    booking = await a_booking(api_client, db_engine, "mv-refund")
    moved = await move(api_client, booking, booking["starts"][2])
    assert moved.status_code == 201, moved.text

    cancelled = await api_client.post(
        f"/api/v1/sessions/{moved.json()['id']}/cancel", json=A_REASON, headers=booking["headers"]
    )

    assert cancelled.status_code == 200, cancelled.text
    assert await balance_of(db_engine, booking["mentee"]) == FUNDED
    assert await ledger(db_engine, booking["mentee"]) == [
        (-1, "session_booked", booking["id"]),
        (1, "session_cancelled_refund", moved.json()["id"]),
    ]


async def test_a_session_moved_twice_still_refunds(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-twice")
    first = await move(api_client, booking, booking["starts"][1])
    assert first.status_code == 201, first.text
    second = await move(api_client, {**booking, "id": first.json()["id"]}, booking["starts"][2])
    assert second.status_code == 201, second.text

    cancelled = await api_client.post(
        f"/api/v1/sessions/{second.json()['id']}/cancel", json=A_REASON, headers=booking["headers"]
    )

    assert cancelled.status_code == 200, cancelled.text
    assert await balance_of(db_engine, booking["mentee"]) == FUNDED


async def test_the_old_time_goes_back_on_the_mentors_grid(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-freed")

    await move(api_client, booking, booking["starts"][2])

    assert booking["starts"][0] in await later_slots(
        api_client, booking["mentor"], booking["session_type"]
    )


async def test_a_pending_request_moves_and_waits_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-pending", mentor_confirms=True)
    assert booking["status"] == "pending_mentor_approval"

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    assert moved.json()["status"] == "pending_mentor_approval"
    assert (await session_row(db_engine, booking["id"]))["status"] == "withdrawn"
    assert (await last_event(db_engine, booking["id"]))["reason_code"] == "rescheduled"


async def test_with_approval_on_the_new_time_waits_and_the_old_one_is_released(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Owner's rule: the mentee gave the old time up, so it is never held."""
    booking = await a_booking(api_client, db_engine, "mv-approval")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = true WHERE user_id = :u"
            ),
            {"u": booking["mentor"]},
        )

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    assert moved.json()["status"] == "pending_mentor_approval"
    assert (await session_row(db_engine, booking["id"]))["status"] == "cancelled"
    assert booking["starts"][0] in await later_slots(
        api_client, booking["mentor"], booking["session_type"]
    )


async def test_one_booked_message_and_nothing_about_the_old_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No cancellation email beside the new booking's."""
    booking = await a_booking(api_client, db_engine, "mv-message")

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    assert await messages_about(db_engine, moved.json()["id"]) == ["session_booked"]
    assert await messages_about(db_engine, booking["id"]) == ["session_booked"]


async def test_a_move_needing_approval_asks_the_mentor_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-message-pending", mentor_confirms=True)

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    assert await messages_about(db_engine, moved.json()["id"]) == ["session_requested"]
    assert await messages_about(db_engine, booking["id"]) == ["session_requested"]


async def test_a_moved_request_declined_refunds_to_the_part_it_came_from(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The /me split follows the move back to the lot that paid (decision 232):
    a monthly credit comes back as monthly, where an unlinked refund lot would
    be bonus."""
    booking = await a_booking(api_client, db_engine, "mv-bucket", mentor_confirms=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE credit_lots SET source = 'monthly_free', expires_at = :e, "
                "quantity_remaining = 0 WHERE user_id = :u"
            ),
            {"e": dt.datetime(2099, 1, 1, tzinfo=dt.UTC), "u": booking["mentee"]},
        )
    moved = await move(api_client, booking, booking["starts"][2])
    assert moved.status_code == 201, moved.text

    declined = await api_client.post(
        f"/api/v1/sessions/{moved.json()['id']}/decline",
        json=A_REASON,
        headers=bearer(await token_for(db_engine, booking["mentor"])),
    )

    assert declined.status_code == 200, declined.text
    async with create_session_factory(db_engine)() as db:
        result = await get_credit_summary(
            db, booking["mentee"], ladder=credit_ladder(Settings(_env_file=None))
        )
    assert (result.monthly.balance, result.bonus.balance) == (1, 0)


async def test_the_new_session_gets_its_venue_and_the_old_event_is_released(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fakes: tuple[FakeRooms, FakeCalendar],  # noqa: F811 - the imported fixture
) -> None:
    """The venue is provisioned for the new session as booking provisions it."""
    _, calendar = fakes
    setup = await a_mentor_on(db_engine, "mv-venue", "daily")
    booked = await book_on(api_client, setup)
    old_event = (await venue_of(db_engine, booked["id"]))["external_calendar_event_id"]
    starts = await later_slots(api_client, setup["mentor"], setup["session_type"])

    moved = await api_client.post(
        f"/api/v1/sessions/{booked['id']}/move",
        json={"starts_at": starts[0]},
        headers=setup["mentee_headers"] | key(),
    )

    assert moved.status_code == 201, moved.text
    assert (await venue_of(db_engine, moved.json()["id"]))["meeting_url"] == FAKE_ROOM
    assert old_event is None or old_event in calendar.asked_to_cancel


async def test_the_intake_carries_over_to_the_new_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-intake", question=True)

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    answers = await api_client.get(
        f"/api/v1/sessions/{moved.json()['id']}/answers", headers=booking["headers"]
    )
    assert answers.status_code == 200, answers.text
    assert [a["text"] for a in answers.json()["data"]] == ["Help with my essay"]


async def test_an_attached_file_moves_to_the_new_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Its mentor reads the file through the new session; the mentee never
    uploads it again."""
    booking = await a_booking(api_client, db_engine, "mv-file", question=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO intake_files (uploader_id, session_id, storage_key, filename, "
                " size_bytes, content_type) "
                "VALUES (:u, :s, 'mv-file-key', 'sop.pdf', 10, 'application/pdf')"
            ),
            {"u": booking["mentee"], "s": booking["id"]},
        )

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text
    async with db_engine.connect() as conn:
        attached = (
            await conn.execute(
                text("SELECT session_id FROM intake_files WHERE storage_key = 'mv-file-key'")
            )
        ).scalar_one()
    assert str(attached) == moved.json()["id"]


async def test_the_mentees_message_reaches_the_mentor_on_the_new_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """As the new session's note, and on the old session's event."""
    booking = await a_booking(api_client, db_engine, "mv-note")

    moved = await move(api_client, booking, booking["starts"][2], message="Clash at work, sorry")

    assert moved.status_code == 201, moved.text
    assert moved.json()["booking_message"] == "Clash at work, sorry"
    async with db_engine.connect() as conn:
        written = (
            await conn.execute(
                text(
                    "SELECT reason_text FROM session_events WHERE session_id = :i "
                    "AND reason_code = 'rescheduled'"
                ),
                {"i": booking["id"]},
            )
        ).scalar_one()
    assert written == "Clash at work, sorry"


async def test_a_blank_message_keeps_the_old_note(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-note-kept")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET booking_message = 'My essay draft' WHERE id = :i"),
            {"i": booking["id"]},
        )

    moved = await move(api_client, booking, booking["starts"][2], message="   ")

    assert moved.status_code == 201, moved.text
    assert moved.json()["booking_message"] == "My essay draft"
    assert moved.json()["topic"] == "Personal statement"
    assert (await last_event(db_engine, booking["id"]))["reason_code"] == "rescheduled"
    async with db_engine.connect() as conn:
        written = (
            await conn.execute(
                text(
                    "SELECT reason_text FROM session_events WHERE session_id = :i "
                    "AND reason_code = 'rescheduled'"
                ),
                {"i": booking["id"]},
            )
        ).scalar_one()
    assert written is None


async def test_a_retried_move_replays_the_first_answer(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-replay")
    same = key()

    first = await move(api_client, booking, booking["starts"][2], idempotency=same)
    again = await move(api_client, booking, booking["starts"][2], idempotency=same)

    assert first.status_code == 201, first.text
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]


# --------------------------------------------------------------------------
# Refusals leave the old session as it was
# --------------------------------------------------------------------------


async def assert_untouched(engine: AsyncEngine, booking: dict[str, Any]) -> None:
    old = await session_row(engine, booking["id"])
    assert old["status"] == booking["status"]
    assert await balance_of(engine, booking["mentee"]) == FUNDED - 1
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM sessions WHERE mentee_id = :u"),
                {"u": booking["mentee"]},
            )
        ).scalar_one()
    assert count == 1


async def test_a_time_not_offered_is_refused_and_nothing_changes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-not-offered")
    odd = (dt.datetime.fromisoformat(booking["starts"][2]) + dt.timedelta(minutes=7)).isoformat()

    moved = await move(api_client, booking, odd)

    assert moved.status_code == 422, moved.text
    await assert_untouched(db_engine, booking)


async def test_a_time_another_mentee_holds_is_refused_and_nothing_changes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-taken")
    _, other = await a_mentee(db_engine, "mv-taken-other")
    taken = await api_client.post(
        "/api/v1/sessions",
        json=body(booking["session_type"], booking["starts"][2]),
        headers=other | key(),
    )
    assert taken.status_code == 201, taken.text

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 422, moved.text
    await assert_untouched(db_engine, booking)


async def test_a_move_inside_the_cutoff_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-cutoff")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() + interval '5 minutes' WHERE id = :i"),
            {"i": booking["id"]},
        )

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 409, moved.text
    assert (await session_row(db_engine, booking["id"]))["status"] == "confirmed"


async def test_the_mentor_cannot_move_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The mentee's action; to the mentor it does not exist, as with withdraw."""
    booking = await a_booking(api_client, db_engine, "mv-mentor")
    mentor_headers = bearer(await token_for(db_engine, booking["mentor"]))

    moved = await move(api_client, booking, booking["starts"][2], headers=mentor_headers)

    assert moved.status_code == 404, moved.text
    await assert_untouched(db_engine, booking)


async def test_a_stranger_cannot_move_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-stranger")
    _, stranger = await a_mentee(db_engine, "mv-stranger-other")

    moved = await move(api_client, booking, booking["starts"][2], headers=stranger)

    assert moved.status_code == 404, moved.text
    await assert_untouched(db_engine, booking)


async def test_a_confirmed_session_inside_the_refund_window_cannot_move(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Moving it would carry a credit a cancellation would forfeit."""
    booking = await a_booking(api_client, db_engine, "mv-too-late")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() + interval '11 hours' WHERE id = :i"),
            {"i": booking["id"]},
        )

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 409, moved.text
    assert moved.json()["type"] == "/problems/move-too-late"
    assert (await session_row(db_engine, booking["id"]))["status"] == "confirmed"


async def test_a_confirmed_session_just_outside_the_window_moves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-in-time")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET starts_at = now() + interval '12 hours 5 minutes' "
                "WHERE id = :i"
            ),
            {"i": booking["id"]},
        )

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text


async def test_a_pending_request_moves_however_close(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Withdrawing a request always refunds, so it may always move."""
    booking = await a_booking(api_client, db_engine, "mv-pending-close", mentor_confirms=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET starts_at = now() + interval '2 hours', respond_by = NULL "
                "WHERE id = :i"
            ),
            {"i": booking["id"]},
        )

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 201, moved.text


async def test_an_ended_session_cannot_be_moved(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(api_client, db_engine, "mv-ended")
    cancelled = await api_client.post(
        f"/api/v1/sessions/{booking['id']}/cancel", json=A_REASON, headers=booking["headers"]
    )
    assert cancelled.status_code == 200, cancelled.text

    moved = await move(api_client, booking, booking["starts"][2])

    assert moved.status_code == 409, moved.text


async def test_the_published_contract_never_promises_a_credit_refusal(
    api_client: httpx.AsyncClient,
) -> None:
    """A move spends no credit, so its 409 must not list one."""
    spec = (await api_client.get("/openapi.json")).json()
    conflict = spec["paths"]["/api/v1/sessions/{session_id}/move"]["post"]["responses"]["409"]

    assert "/problems/move-too-late" in conflict["description"]
    assert "Never `/problems/insufficient-credit`" in conflict["description"]

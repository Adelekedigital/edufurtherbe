"""A mentee reminds the mentor about a pending request (decision 294).

`POST /sessions/{id}/reminder` is the mentee's, once, while the request still
awaits an answer. It stamps `reminder_sent_at` and queues **one** email to the
mentor carrying what the mentee wrote. Weighted towards the refusals, as the
transitions are: the success is a few lines and would pass against an endpoint
that checked nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.test_api_mentee_attendance_rate import a_pair
from tests.integration.test_api_missed_reasons import a_stranger

from app.core.errors import ConfigurationError
from app.domain.notifications import Notification
from app.domain.request_reminders import REMINDER_MESSAGE_MAX_LENGTH
from app.infra.db.outbox import MAX_ATTEMPTS, STILL_DUE, drain, enqueue
from app.infra.db.pending_requests import request_reminder_state

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

NOTIFICATION = "mentee_request_reminder"


async def a_request(
    engine: AsyncEngine,
    pair: dict[str, Any],
    *,
    status: str = "pending_mentor_approval",
    respond_in: dt.timedelta | None = dt.timedelta(hours=10),
    starts_in: dt.timedelta = dt.timedelta(days=2),
) -> UUID:
    """A session between the pair; ``respond_in=None`` leaves `respond_by` null."""
    now = dt.datetime.now(dt.UTC)
    async with engine.begin() as conn:
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, "
                    "status, respond_by) VALUES (:m, :e, :s, 45, :status, :r) RETURNING id"
                ),
                {
                    "m": pair["mentor"],
                    "e": pair["mentee"],
                    "s": now + starts_in,
                    "status": status,
                    "r": None if respond_in is None else now + respond_in,
                },
            )
        ).scalar_one()
    return UUID(str(session_id))


async def remind(
    client: httpx.AsyncClient,
    session_id: UUID,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/sessions/{session_id}/reminder",
        json={"message": "Still keen to meet, thanks!"} if body is None else body,
        headers=headers,
    )


async def queued(engine: AsyncEngine, session_id: UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE entity_id = :s AND event_type = :n ORDER BY created_at"
            ),
            {"s": session_id, "n": NOTIFICATION},
        )
        return [dict(row["payload"]) for row in rows.mappings()]


async def sent_at(engine: AsyncEngine, session_id: UUID) -> dt.datetime | None:
    async with engine.connect() as conn:
        value: dt.datetime | None = (
            await conn.execute(
                text("SELECT reminder_sent_at FROM sessions WHERE id = :s"), {"s": session_id}
            )
        ).scalar_one()
        return value


def problem(response: httpx.Response) -> str:
    return str(response.json()["type"])


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def test_the_mentee_reminds_the_mentor_once_and_the_mentor_is_emailed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "rr-send")
    session_id = await a_request(db_engine, pair)

    sent = await remind(
        api_client, session_id, pair["mentee_headers"], {"message": "  Still keen, thanks!  "}
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["id"] == str(session_id)
    assert body["status"] == "pending_mentor_approval"
    assert body["reminder_sent_at"] is not None
    assert await sent_at(db_engine, session_id) is not None
    rows = await queued(db_engine, session_id)
    assert rows == [
        {"recipient_id": str(pair["mentor"]), "reminder_message": "Still keen, thanks!"}
    ]


async def test_both_parties_read_when_the_reminder_was_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Null until sent, then the same instant on the list and the detail, for
    the mentor as for the mentee."""
    pair = await a_pair(db_engine, "rr-read")
    session_id = await a_request(db_engine, pair)
    before = await api_client.get(f"/api/v1/sessions/{session_id}", headers=pair["mentee_headers"])
    assert before.json()["reminder_sent_at"] is None

    stamp = (await remind(api_client, session_id, pair["mentee_headers"])).json()[
        "reminder_sent_at"
    ]

    for headers, user in (
        (pair["mentee_headers"], pair["mentee"]),
        (pair["mentor_headers"], pair["mentor"]),
    ):
        detail = await api_client.get(f"/api/v1/sessions/{session_id}", headers=headers)
        assert detail.json()["reminder_sent_at"] == stamp
        listed = await api_client.get(f"/api/v1/users/{user}/sessions", headers=headers)
        [row] = [r for r in listed.json()["data"] if r["id"] == str(session_id)]
        assert row["reminder_sent_at"] == stamp


# --------------------------------------------------------------------------
# Who may send it
# --------------------------------------------------------------------------


async def test_the_mentor_is_refused_with_a_403_and_nothing_is_written(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A party knows the session exists, so the refusal discloses nothing
    (decision 287's rule for this API's 403)."""
    pair = await a_pair(db_engine, "rr-mentor")
    session_id = await a_request(db_engine, pair)

    refused = await remind(api_client, session_id, pair["mentor_headers"])

    assert refused.status_code == 403, refused.text
    assert problem(refused) == "/problems/reminder-mentee-only"
    assert await sent_at(db_engine, session_id) is None
    assert await queued(db_engine, session_id) == []


async def test_a_stranger_and_an_unknown_id_are_both_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "rr-stranger")
    session_id = await a_request(db_engine, pair)
    _, stranger = await a_stranger(db_engine, "rr-stranger-x")

    assert (await remind(api_client, session_id, stranger)).status_code == 404
    assert (await remind(api_client, uuid4(), pair["mentee_headers"])).status_code == 404
    assert await sent_at(db_engine, session_id) is None
    assert await queued(db_engine, session_id) == []


# --------------------------------------------------------------------------
# When it may be sent
# --------------------------------------------------------------------------


async def test_a_second_reminder_is_a_409_and_sends_nothing_more(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "rr-twice")
    session_id = await a_request(db_engine, pair)
    first = await remind(api_client, session_id, pair["mentee_headers"])
    stamp = await sent_at(db_engine, session_id)

    again = await remind(api_client, session_id, pair["mentee_headers"])

    assert first.status_code == 200
    assert again.status_code == 409, again.text
    assert problem(again) == "/problems/reminder-already-sent"
    assert await sent_at(db_engine, session_id) == stamp
    assert len(await queued(db_engine, session_id)) == 1


@pytest.mark.parametrize(
    "status", ["confirmed", "declined", "withdrawn", "expired", "cancelled", "completed"]
)
async def test_a_request_no_longer_pending_is_a_409(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    pair = await a_pair(db_engine, f"rr-{status}")
    session_id = await a_request(db_engine, pair, status=status)

    refused = await remind(api_client, session_id, pair["mentee_headers"])

    assert refused.status_code == 409, refused.text
    assert problem(refused) == "/problems/request-not-pending"
    assert await queued(db_engine, session_id) == []


async def test_a_request_past_its_deadline_is_a_409_even_before_the_sweep(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Still `pending_mentor_approval` in the table, but the mentor can no
    longer answer it, so there is nothing to remind them about."""
    pair = await a_pair(db_engine, "rr-lapsed")
    session_id = await a_request(db_engine, pair, respond_in=-dt.timedelta(minutes=1))

    refused = await remind(api_client, session_id, pair["mentee_headers"])

    assert refused.status_code == 409, refused.text
    assert problem(refused) == "/problems/request-not-pending"
    assert await sent_at(db_engine, session_id) is None


async def test_a_migrated_request_with_no_deadline_can_be_reminded_until_it_starts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No `respond_by` means the request lapses at `starts_at` (decision 121)."""
    pair = await a_pair(db_engine, "rr-legacy")
    open_one = await a_request(db_engine, pair, respond_in=None)
    started = await a_request(db_engine, pair, respond_in=None, starts_in=-dt.timedelta(minutes=5))

    assert (await remind(api_client, open_one, pair["mentee_headers"])).status_code == 200
    late = await remind(api_client, started, pair["mentee_headers"])
    assert late.status_code == 409
    assert problem(late) == "/problems/request-not-pending"


async def test_a_reminder_already_sent_on_an_answered_request_says_not_pending(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """When both apply, the state of the request is what the mentee needs to hear."""
    pair = await a_pair(db_engine, "rr-both")
    session_id = await a_request(db_engine, pair)
    await remind(api_client, session_id, pair["mentee_headers"])
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'confirmed' WHERE id = :s"), {"s": session_id}
        )

    refused = await remind(api_client, session_id, pair["mentee_headers"])

    assert problem(refused) == "/problems/request-not-pending"


async def test_two_reminders_at_once_send_one_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A double tap: the conditional write is the guard, so exactly one wins."""
    pair = await a_pair(db_engine, "rr-race")
    session_id = await a_request(db_engine, pair)

    responses = await asyncio.gather(
        remind(api_client, session_id, pair["mentee_headers"]),
        remind(api_client, session_id, pair["mentee_headers"]),
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(await queued(db_engine, session_id)) == 1


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": "x" * (REMINDER_MESSAGE_MAX_LENGTH + 1)},
        {"message": "hi", "unexpected": True},
    ],
    ids=["missing", "empty", "blank", "too-long", "extra-field"],
)
async def test_a_bad_message_is_a_422_and_nothing_is_written(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, Any]
) -> None:
    pair = await a_pair(db_engine, f"rr-422-{len(body)}-{len(str(body))}")
    session_id = await a_request(db_engine, pair)

    refused = await remind(api_client, session_id, pair["mentee_headers"], body)

    assert refused.status_code == 422, refused.text
    assert await sent_at(db_engine, session_id) is None


async def test_a_message_of_exactly_the_limit_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "rr-limit")
    session_id = await a_request(db_engine, pair)
    message = "x" * REMINDER_MESSAGE_MAX_LENGTH

    sent = await remind(api_client, session_id, pair["mentee_headers"], {"message": message})

    assert sent.status_code == 200, sent.text
    assert (await queued(db_engine, session_id))[0]["reminder_message"] == message


# --------------------------------------------------------------------------
# At the drain
# --------------------------------------------------------------------------


async def test_the_email_is_still_due_only_while_the_request_awaits_an_answer(
    db_engine: AsyncEngine,
) -> None:
    """Queued, then answered before the drain reached it: the mentor is not
    asked to answer something they already answered."""
    pair = await a_pair(db_engine, "rr-drain")
    waiting = await a_request(db_engine, pair)
    answered = await a_request(db_engine, pair, status="confirmed", starts_in=dt.timedelta(days=3))
    lapsed = await a_request(
        db_engine, pair, respond_in=-dt.timedelta(minutes=1), starts_in=dt.timedelta(days=4)
    )
    now = dt.datetime.now(dt.UTC)

    # The drain asks exactly this before sending.
    assert STILL_DUE[Notification.MENTEE_REQUEST_REMINDER] is request_reminder_state
    async with AsyncSession(db_engine) as session:
        assert await request_reminder_state(session, waiting, {}, now) == "due"
        assert await request_reminder_state(session, answered, {}, now) == "stale"
        assert await request_reminder_state(session, lapsed, {}, now) == "stale"
        assert await request_reminder_state(session, uuid4(), {}, now) == "stale"


class Unmapped:
    """A notifier whose template for this message is not configured yet, as
    `template_for` refuses it before anything is sent."""

    def send(self, **kwargs: Any) -> None:
        raise ConfigurationError(f"no email template configured for {kwargs['notification']}")


class Recorder:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


async def drain_one(engine: AsyncEngine, session_id: UUID, notifier: Any) -> dict[str, int]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        counts = await drain(
            session, notifier=notifier, now=dt.datetime.now(dt.UTC), entity_id=session_id
        )
        await session.commit()
    return counts


async def row_state(engine: AsyncEngine, session_id: UUID) -> tuple[str, int]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, attempts FROM outbox_events "
                    "WHERE entity_id = :s AND event_type = :n"
                ),
                {"s": session_id, "n": NOTIFICATION},
            )
        ).one()
        return str(row.status), int(row.attempts)


async def test_a_reminder_waits_for_its_template_instead_of_spending_its_attempts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The mentee's one reminder is stamped the moment they send it, so it must
    not be lost to a template the operator has not mapped yet: it waits, with
    no attempt counted, and goes out once the template exists."""
    pair = await a_pair(db_engine, "rr-unmapped")
    session_id = await a_request(db_engine, pair)
    await remind(api_client, session_id, pair["mentee_headers"])

    for _ in range(MAX_ATTEMPTS + 2):
        await drain_one(db_engine, session_id, Unmapped())

    assert await row_state(db_engine, session_id) == ("pending", 0)
    mapped = Recorder()
    await drain_one(db_engine, session_id, mapped)
    assert [m["notification"] for m in mapped.sent] == [Notification.MENTEE_REQUEST_REMINDER]
    assert await row_state(db_engine, session_id) == ("sent", 1)


async def test_a_waiting_reminder_is_dropped_once_the_request_is_answered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Waiting is safe only because the freshness check runs first on every
    sweep: an answer given while the template was missing ends the reminder."""
    pair = await a_pair(db_engine, "rr-unmapped-answered")
    session_id = await a_request(db_engine, pair)
    await remind(api_client, session_id, pair["mentee_headers"])
    await drain_one(db_engine, session_id, Unmapped())
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'confirmed' WHERE id = :s"), {"s": session_id}
        )

    mapped = Recorder()
    await drain_one(db_engine, session_id, mapped)

    assert mapped.sent == []
    assert (await row_state(db_engine, session_id))[0] == "skipped"


async def test_an_unmapped_message_with_no_freshness_check_keeps_its_bounded_retry(
    db_engine: AsyncEngine,
) -> None:
    """Waiting is only for messages re-checked at send time. One that is not
    (here the booking request itself) would go out late once mapped, so it
    still counts the attempt."""
    pair = await a_pair(db_engine, "rr-unmapped-other")
    session_id = await a_request(db_engine, pair)
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await enqueue(
            session,
            Notification.SESSION_REQUESTED,
            entity_type="session",
            entity_id=session_id,
            recipient_ids=(pair["mentor"],),
        )
        await session.commit()

    counts = await drain_one(db_engine, session_id, Unmapped())

    assert counts["failed"] == 1
    async with db_engine.connect() as conn:
        attempts = (
            await conn.execute(
                text("SELECT attempts FROM outbox_events WHERE entity_id = :s"), {"s": session_id}
            )
        ).scalar_one()
    assert attempts == 1

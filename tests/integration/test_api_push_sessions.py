"""Booking and session pushes end to end (decision 312).

A push row is queued beside the email for whoever has a browser, and sent as
the request that caused it responds, or by the reminder callback. The fake
push services stand on `app.state.web_push`, the seam the code reads.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_notifications import Recorder, a_booking, queued
from tests.integration.test_api_push_messages import FakePush, _configure
from tests.integration.test_api_reminder_callback import PATH, believing_client, signed_headers
from tests.unit.test_push_config import SUBJECT, vapid_pair

from app.core.config import Settings
from app.domain.notifications import Channel
from app.domain.push import PUSH_TTL, SESSION_SOON_TTL
from app.infra.clients.web_push import PushOutcome
from app.infra.db.outbox import drain, drain_push
from app.infra.jobs import runner
from app.infra.jobs.runner import RuntimeJobs
from conftest import A_REASON


def tags(campaign: str) -> str:
    """A session push link's tags (decision 336)."""
    return f"utm_source=push&utm_medium=notification&utm_campaign={campaign}"


pytestmark = [pytest.mark.db, pytest.mark.asyncio]

FCM = "https://fcm.googleapis.com/fcm/send/"


async def browser(engine: AsyncEngine, user: UUID) -> str:
    """A subscribed browser for `user`, written as `push_store.subscribe` would."""
    endpoint = FCM + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
                "VALUES (:u, :e, :p, :a)"
            ),
            {"u": user, "e": endpoint, "p": "B" + "p" * 86, "a": "a" * 22},
        )
    return endpoint


def pushed(client: httpx.AsyncClient, *, on: bool = True) -> FakePush:
    app = _configure(client, push=on)
    fake = FakePush()
    app.state.web_push = fake if on else None
    return fake


async def booked(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, *, confirmation: bool = False
) -> tuple[dict[str, Any], dict[str, str]]:
    """A booking whose mentor and mentee each have a browser."""
    endpoints: dict[str, str] = {}

    async def subscribe(mentor: UUID, mentee: UUID) -> None:
        endpoints["mentor"] = await browser(engine, mentor)
        endpoints["mentee"] = await browser(engine, mentee)

    booking = await a_booking(engine, client, tag, confirmation=confirmation, before=subscribe)
    return booking, endpoints


def minutes(ttl: dt.timedelta) -> dt.timedelta:
    """A sent TTL is what is left of the push's life, so a few milliseconds
    short of it: rounded up to the minute for comparing."""
    return dt.timedelta(minutes=math.ceil(ttl.total_seconds() / 60))


def pushes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["destination"] == Channel.PUSH]


async def test_a_request_pushes_the_mentor_only(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    fake = pushed(api_client)
    booking, endpoints = await booked(db_engine, api_client, "ps-request", confirmation=True)

    sid = booking["id"]
    assert [(e, b, minutes(ttl), u) for e, b, ttl, u in fake.sent] == [
        (
            endpoints["mentor"],
            {
                "type": "booking_request",
                "title": "New booking request",
                "body": "Mo wants to book a session with you",
                "url": f"/bookings?tab=pending&booking={sid}&{tags('session_requested')}",
                "tag": f"session:{sid}",
                "badge": 0,
            },
            PUSH_TTL,
            "normal",
        )
    ]
    rows = pushes(await queued(db_engine, sid))
    assert [(row["status"], row["attempts"]) for row in rows] == [("sent", 1)]


async def test_an_auto_confirmed_booking_never_pushes_the_mentee_who_made_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The email goes to both, for the join link; the push only to the mentor."""
    fake = pushed(api_client)
    booking, endpoints = await booked(db_engine, api_client, "ps-auto")

    assert [(endpoint, body["type"], body["body"]) for endpoint, body, *_ in fake.sent] == [
        (endpoints["mentor"], "booking_request", "Mo booked a session with you")
    ]
    statuses = {
        row["payload"]["recipient_id"]: (row["status"], row["error_detail"])
        for row in pushes(await queued(db_engine, booking["id"]))
    }
    # No row for the mentee at all: who is pushed is decided as it is queued.
    assert statuses == {str(booking["mentor"]): ("sent", None)}


async def test_nobody_with_a_browser_means_no_push_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    fake = pushed(api_client)
    booking = await a_booking(db_engine, api_client, "ps-none")

    assert fake.sent == []
    assert pushes(await queued(db_engine, booking["id"])) == []


async def test_accepting_pushes_the_mentee_with_the_mentors_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    fake = pushed(api_client)
    booking, endpoints = await booked(db_engine, api_client, "ps-accept", confirmation=True)
    fake.sent.clear()

    answered = await api_client.post(
        f"/api/v1/sessions/{booking['id']}/accept", headers=booking["mentor_headers"]
    )

    assert answered.status_code == 200, answered.text
    assert [(endpoint, body["type"], body["body"]) for endpoint, body, *_ in fake.sent] == [
        (endpoints["mentee"], "booking_response", "Ada accepted your booking")
    ]


async def test_a_cancellation_pushes_the_other_party_with_their_unread_badge(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    fake = pushed(api_client)
    booking, endpoints = await booked(db_engine, api_client, "ps-cancel")
    said = await api_client.post(
        f"/api/v1/me/messages/{booking['mentor']}",
        json={"body": "Running late", "client_id": str(uuid4())},
        headers=booking["mentee_headers"],
    )
    assert said.status_code == 201, said.text
    fake.sent.clear()

    cancelled = await api_client.post(
        f"/api/v1/sessions/{booking['id']}/cancel",
        headers=booking["mentee_headers"],
        json=A_REASON,
    )

    assert cancelled.status_code == 200, cancelled.text
    assert [
        (endpoint, body["type"], body["title"], body["body"], body["badge"])
        for endpoint, body, *_ in fake.sent
    ] == [
        (
            endpoints["mentor"],
            "session_changed",
            "Session cancelled",
            "Mo cancelled your session",
            1,
        )
    ]


async def test_with_push_off_the_row_waits_and_expires_past_its_lifetime(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Off, the request sends nothing and the row stays pending; a sweep after
    its lifetime skips it rather than alerting about a day-old booking."""
    pushed(api_client, on=False)
    booking, _ = await booked(db_engine, api_client, "ps-off", confirmation=True)
    assert [row["status"] for row in pushes(await queued(db_engine, booking["id"]))] == ["pending"]
    fake = FakePush()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        late = dt.datetime.now(dt.UTC) + PUSH_TTL + dt.timedelta(minutes=1)
        counts = await drain_push(session, web_push=fake, now=late, entity_id=booking["id"])
        await session.commit()

    assert (counts, fake.sent) == ({"sent": 0, "failed": 0, "skipped": 1}, [])
    assert [
        (row["status"], row["error_detail"])
        for row in pushes(await queued(db_engine, booking["id"]))
    ] == [("skipped", "expired")]


async def test_a_move_pushes_as_a_reschedule(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A move sends the booking's own email (decision 300); its push says moved."""
    pushed(api_client, on=False)
    earlier = await a_booking(db_engine, api_client, "ps-moved-from")
    booking, endpoints = await booked(db_engine, api_client, "ps-moved", confirmation=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET rescheduled_from_session_id = :f WHERE id = :i"),
            {"f": earlier["id"], "i": booking["id"]},
        )
    fake = FakePush()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await drain_push(
            session, web_push=fake, now=dt.datetime.now(dt.UTC), entity_id=booking["id"]
        )
        await session.commit()

    assert [(endpoint, body["type"], body["body"]) for endpoint, body, *_ in fake.sent] == [
        (endpoints["mentor"], "session_changed", "Mo asked to move your session")
    ]


async def test_a_gone_browser_is_forgotten_and_a_refused_push_is_not_retried(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pushed(api_client, on=False)
    gone, _ = await booked(db_engine, api_client, "ps-gone", confirmation=True)
    refused, refused_at = await booked(db_engine, api_client, "ps-refused", confirmation=True)
    fake = FakePush()
    async with db_engine.connect() as conn:
        gone_endpoint = (
            await conn.execute(
                text("SELECT endpoint FROM push_subscriptions WHERE user_id = :u"),
                {"u": gone["mentor"]},
            )
        ).scalar_one()
    fake.answers = {
        gone_endpoint: PushOutcome.GONE,
        refused_at["mentor"]: PushOutcome.FAILED,
    }

    for _ in range(2):  # the second sweep finds nothing left to try
        async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
            await drain_push(session, web_push=fake, now=dt.datetime.now(dt.UTC))
            await session.commit()

    assert len(fake.sent) == 2
    assert [
        (row["status"], row["attempts"]) for row in pushes(await queued(db_engine, gone["id"]))
    ] == [("skipped", 1)]
    assert [
        (row["status"], row["attempts"]) for row in pushes(await queued(db_engine, refused["id"]))
    ] == [("failed", 1)]
    async with db_engine.connect() as conn:
        left = (
            await conn.execute(
                text("SELECT count(*) FROM push_subscriptions WHERE user_id = :u"),
                {"u": gone["mentor"]},
            )
        ).scalar_one()
    assert left == 0


async def test_the_five_minute_reminder_pushes_both_from_its_callback_urgently(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking, endpoints = await booked(db_engine, api_client, "ps-soon")
    callback_client = believing_client(db_engine)
    app = callback_client._transport.app  # type: ignore[attr-defined]
    public, private = vapid_pair()
    app.state.settings = app.state.settings.model_copy(
        update={
            "vapid_public_key": public,
            "vapid_private_key": SecretStr(private),
            "vapid_subject": SUBJECT,
        }
    )
    fake = FakePush()
    app.state.web_push = fake
    body = json.dumps({"session_id": booking["id"], "kind": "s5"}).encode()

    answered = await callback_client.post(PATH, content=body, headers=signed_headers(body))

    assert answered.status_code == 200, answered.text
    sid = booking["id"]
    assert sorted(
        (endpoint, b["type"], b["body"], minutes(ttl), urgency)
        for endpoint, b, ttl, urgency in fake.sent
    ) == sorted(
        [
            (
                endpoints["mentor"],
                "session_soon",
                "Your session with Mo starts in 5 minutes",
                SESSION_SOON_TTL,
                "high",
            ),
            (
                endpoints["mentee"],
                "session_soon",
                "Your session with Ada starts in 5 minutes",
                SESSION_SOON_TTL,
                "high",
            ),
        ]
    )
    assert {b["url"] for _, b, *_ in fake.sent} == {
        f"/sessions/{sid}?{tags('session_last_reminder')}"
    }


async def test_the_email_sweep_leaves_the_push_rows_to_their_own(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pushed(api_client, on=False)
    booking, _ = await booked(db_engine, api_client, "ps-email", confirmation=True)
    recorder = Recorder()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await drain(session, notifier=recorder, now=dt.datetime.now(dt.UTC))
        await session.commit()

    assert [sent["channel"] for sent in recorder.sent] == [Channel.EMAIL]
    assert [row["status"] for row in pushes(await queued(db_engine, booking["id"]))] == ["pending"]


async def test_a_suggestion_no_longer_open_is_not_pushed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`STILL_DUE` holds for the push as for the email: no open offer, no push."""
    pushed(api_client, on=False)
    booking, _ = await booked(db_engine, api_client, "ps-stale", confirmation=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox_events (event_type, entity_type, entity_id, destination, "
                "payload) VALUES ('session_time_suggested', 'session', :i, 'push', "
                "jsonb_build_object('recipient_id', CAST(:m AS text)))"
            ),
            {"i": booking["id"], "m": str(booking["mentee"])},
        )
    fake = FakePush()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await drain_push(
            session, web_push=fake, now=dt.datetime.now(dt.UTC), entity_id=booking["id"]
        )
        await session.commit()

    rows = pushes(await queued(db_engine, booking["id"]))
    assert {(row["event_type"], row["status"], row["error_detail"]) for row in rows} == {
        ("session_requested", "sent", None),
        ("session_time_suggested", "skipped", "superseded"),
    }
    assert [body["type"] for _, body, *_ in fake.sent] == ["booking_request"]


@pytest.mark.parametrize("dry_run", [False, True])
async def test_the_hourly_sweep_sends_what_a_request_left_and_a_dry_run_does_not(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    pushed(api_client, on=False)
    booking, endpoints = await booked(
        db_engine, api_client, f"ps-sweep-{str(dry_run).lower()}", confirmation=True
    )
    fake = FakePush()
    monkeypatch.setattr(runner, "live_web_push", lambda _settings: fake)

    await RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database))).run(  # type: ignore[call-arg]
        "settle-sessions", dry_run=dry_run
    )

    assert [endpoint for endpoint, *_ in fake.sent] == ([] if dry_run else [endpoints["mentor"]])
    assert [row["status"] for row in pushes(await queued(db_engine, booking["id"]))] == [
        "pending" if dry_run else "sent"
    ]


async def test_a_failing_analytics_drain_cannot_unsend_the_pushes_before_it(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Email and push marks are committed before the analytics drain runs, so
    its failure cannot roll them back and send the pushes again next hour."""
    pushed(api_client, on=False)
    booking, endpoints = await booked(
        db_engine, api_client, "ps-analytics-fails", confirmation=True
    )
    fake = FakePush()
    monkeypatch.setattr(runner, "live_web_push", lambda _settings: fake)
    monkeypatch.setattr(runner, "live_analytics_sink", lambda _settings: object())

    async def broken(*_args: object, **_kwargs: object) -> dict[str, int]:
        raise RuntimeError("analytics drain broke")

    monkeypatch.setattr(runner, "drain_analytics", broken)

    with pytest.raises(RuntimeError, match="analytics drain broke"):
        await RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database))).run(  # type: ignore[call-arg]
            "settle-sessions"
        )

    assert [endpoint for endpoint, *_ in fake.sent] == [endpoints["mentor"]]
    assert [row["status"] for row in pushes(await queued(db_engine, booking["id"]))] == ["sent"]


async def test_a_late_send_carries_only_what_is_left_of_its_life(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Sent by a sweep ten hours on, a day-long push may wait fourteen more
    hours at the push service, not a fresh day."""
    pushed(api_client, on=False)
    booking, _ = await booked(db_engine, api_client, "ps-late", confirmation=True)
    fake = FakePush()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=10)
        await drain_push(session, web_push=fake, now=later, entity_id=booking["id"])
        await session.commit()

    assert [minutes(ttl) for _, _, ttl, _ in fake.sent] == [dt.timedelta(hours=14)]

"""A queued message about someone who has since deleted their account (#288).

Names are looked up when the outbox drains, not when it was queued, so a
reminder written while both people were live can go out after one of them left.
The owner decided (2026-09-30) that it is still sent, with "your mentor" or
"your mentee" where the name was: the recipient still needs it, and nobody who
left is named.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_application_alert import a_user, apply_as, grant
from tests.integration.test_api_notifications import Recorder, a_booking, queued, sweep

from app.domain.messages import DELETED_PARTY_LABELS, build_variables
from app.domain.notifications import Notification

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def delete_account(engine: AsyncEngine, user_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": user_id}
        )


async def test_a_deleted_mentee_is_your_mentee_to_the_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "gone-mentee")
    await delete_account(db_engine, booking["mentee"])
    recorder = Recorder()

    counts = await sweep(db_engine, recorder)

    assert counts["sent"] == 1
    (message,) = [m for m in recorder.sent if m["notification"] == Notification.SESSION_BOOKED]
    context = message["context"]
    assert context.mentee_name == DELETED_PARTY_LABELS["mentee"] == "your mentee"
    assert "Mo" not in context.mentee_name
    variables = build_variables(["menteeName", "attendee"], context)
    assert variables == {"menteeName": "your mentee", "attendee": "your mentee"}


async def test_a_deleted_mentor_is_your_mentor_to_the_mentee(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "gone-mentor", confirmation=True)
    await api_client.post(
        f"/api/v1/sessions/{booking['id']}/accept", headers=booking["mentor_headers"]
    )
    await delete_account(db_engine, booking["mentor"])
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (message,) = [m for m in recorder.sent if m["notification"] == Notification.REQUEST_ACCEPTED]
    context = message["context"]
    assert context.mentor_name == "your mentor"
    assert build_variables(["mentorName", "attendee"], context) == {
        "mentorName": "your mentor",
        "attendee": "your mentor",
    }


async def test_a_live_counterpart_is_still_named(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await a_booking(db_engine, api_client, "live-both")
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (message,) = [m for m in recorder.sent if m["notification"] == Notification.SESSION_BOOKED]
    context = message["context"]
    assert context.mentee_name == "Mo"
    assert context.mentor_name not in DELETED_PARTY_LABELS.values()


async def test_a_deleted_recipient_is_sent_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Their address is read through `LIVE`, so there is nowhere to send it."""
    booking = await a_booking(db_engine, api_client, "gone-recipient")
    await delete_account(db_engine, booking["mentor"])
    recorder = Recorder()

    counts = await sweep(db_engine, recorder)

    assert recorder.sent == []
    assert counts["skipped"] == 1
    (row,) = await queued(db_engine, booking["id"])
    assert row["status"] == "skipped"


async def test_a_deleted_applicant_is_your_mentor_to_the_admins(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An application notice drained after the applicant left names nobody (#343).

    An empty name would fail every attempt of a template asking for
    `mentorName`; the session messages' label is what this one uses too.
    """
    approver, _ = await a_user(db_engine, "gone-applicant-admin")
    await grant(db_engine, approver, "mentor_approval")
    applicant, auth_id = await a_user(db_engine, "gone-applicant")
    assert (await apply_as(api_client, applicant, auth_id)).status_code == 201
    await delete_account(db_engine, applicant)
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (message,) = [
        m for m in recorder.sent if m["notification"] == Notification.MENTOR_APPLICATION_RECEIVED
    ]
    context = message["context"]
    assert context.mentor_name == DELETED_PARTY_LABELS["mentor"] == "your mentor"
    assert build_variables(["mentorName"], context) == {"mentorName": "your mentor"}


async def test_a_live_applicant_is_named_to_the_admins(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    approver, _ = await a_user(db_engine, "live-applicant-admin")
    await grant(db_engine, approver, "mentor_approval")
    applicant, auth_id = await a_user(db_engine, "live-applicant")
    assert (await apply_as(api_client, applicant, auth_id)).status_code == 201
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (message,) = [
        m for m in recorder.sent if m["notification"] == Notification.MENTOR_APPLICATION_RECEIVED
    ]
    assert message["context"].mentor_name == "Ada"


async def test_a_context_that_cannot_be_built_is_retried_not_lost(
    db_engine: AsyncEngine,
) -> None:
    """A payload the drain cannot read fails the attempt and stays pending (#343).

    The drain's guard around building the context is what keeps one bad row
    from aborting the run; it must count an attempt and leave the row to retry.
    """
    user, _ = await a_user(db_engine, "bad-expiry")
    async with db_engine.begin() as conn:
        row_id = (
            await conn.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_type, entity_type, entity_id, payload, destination, status) "
                    "VALUES ('credits_expiring', 'user', :u, CAST(:p AS jsonb), 'email', "
                    "'pending') RETURNING id"
                ),
                {"u": user, "p": f'{{"recipient_id": "{user}", "expires_at": "not-a-date"}}'},
            )
        ).scalar_one()
    recorder = Recorder()

    counts = await sweep(db_engine, recorder)

    assert recorder.sent == []
    assert counts["failed"] == 1
    async with db_engine.connect() as conn:
        status, attempts, error = (
            await conn.execute(
                text("SELECT status, attempts, error_detail FROM outbox_events WHERE id = :i"),
                {"i": row_id},
            )
        ).one()
    assert (status, attempts) == ("pending", 1)
    assert error

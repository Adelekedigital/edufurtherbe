"""The team's note on a mentor decision (decision 350).

Stored on the decision's own event (`mentor_status_events.reason`, which
already says who and when) and carried to the mentor's email as `team_note`.
A decline still sends `reason`, which the live template reads.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import applicant
from tests.integration.test_api_admin import queued_for
from tests.integration.test_api_admin_decisions import deciding_admin

from app.api.schemas.common import TEAM_NOTE_MAX_LENGTH
from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ADMIN = "/api/v1/admin"
NOTE = "Welcome aboard!\nStart by adding your availability."


async def events(engine: AsyncEngine, mentor: UUID) -> list[tuple[str, str | None]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT status_type, reason FROM mentor_status_events "
                "WHERE mentor_user_id = :m ORDER BY status_type"
            ),
            {"m": mentor},
        )
        return [(str(kind), reason) for kind, reason in rows]


async def decide(
    client: httpx.AsyncClient, token: str, mentor: UUID, body: dict[str, Any], *, approve: bool
) -> httpx.Response:
    return await client.post(
        f"{ADMIN}/mentors/{mentor}/decision",
        params={"approve": str(approve).lower()},
        json=body,
        headers=bearer(token),
    )


def pointers(response: httpx.Response) -> list[str]:
    return [error["pointer"] for error in response.json()["errors"]]


async def test_an_approval_carries_the_note_to_its_event_and_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "note-approve")

    response = await decide(api_client, token, mentor, {"message": f"  {NOTE}  "}, approve=True)

    assert response.status_code == 200, response.text
    assert await events(db_engine, mentor) == [("approved", NOTE), ("listed", None)]
    assert await queued_for(db_engine, mentor) == [("mentor_approved", {"team_note": NOTE})]


async def test_an_approval_with_no_note_is_as_before(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "note-none")

    response = await decide(api_client, token, mentor, {"message": "   "}, approve=True)

    assert response.status_code == 200, response.text
    assert await events(db_engine, mentor) == [("approved", None), ("listed", None)]
    assert await queued_for(db_engine, mentor) == [("mentor_approved", {})]


async def test_a_decline_sends_reason_as_before_and_the_note(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "note-decline")

    response = await decide(api_client, token, mentor, {"message": NOTE}, approve=False)

    assert response.status_code == 200, response.text
    assert await events(db_engine, mentor) == [("declined", NOTE), ("unlisted", "never_approved")]
    assert await queued_for(db_engine, mentor) == [
        ("mentor_declined", {"reason": NOTE, "team_note": NOTE})
    ]


async def test_a_decline_with_no_note_sends_an_empty_reason_as_before(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "note-decline-none")

    await decide(api_client, token, mentor, {}, approve=False)

    assert await queued_for(db_engine, mentor) == [("mentor_declined", {"reason": ""})]


async def test_the_same_text_as_message_and_reason_is_one_note(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "note-both")

    response = await decide(
        api_client, token, mentor, {"message": "Too thin", "reason": " Too thin "}, approve=False
    )

    assert response.status_code == 200, response.text
    assert ("declined", "Too thin") in await events(db_engine, mentor)


@pytest.mark.parametrize(
    "body",
    [
        {"message": "One", "reason": "Two"},
        {"message": "x" * (TEAM_NOTE_MAX_LENGTH + 1)},
    ],
    ids=["disagree", "too-long"],
)
async def test_a_bad_note_is_a_422_at_message_and_decides_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, Any]
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, f"note-bad-{len(body)}")

    response = await decide(api_client, token, mentor, body, approve=True)

    assert response.status_code == 422
    assert pointers(response) == ["/message"]
    assert await events(db_engine, mentor) == []


async def test_a_bulk_approval_gives_each_mentor_the_note(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    first = await applicant(db_engine, "note-bulk-1")
    second = await applicant(db_engine, "note-bulk-2")

    response = await api_client.post(
        f"{ADMIN}/mentors/approvals",
        json={"user_ids": [str(first), str(second)], "message": f" {NOTE} "},
        headers=bearer(token),
    )

    assert response.status_code == 200, response.text
    for mentor in (first, second):
        assert await events(db_engine, mentor) == [("approved", NOTE), ("listed", None)]
        assert await queued_for(db_engine, mentor) == [("mentor_approved", {"team_note": NOTE})]


async def test_a_bulk_note_over_the_limit_is_a_422_at_message(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    token, _ = await deciding_admin(db_engine)
    mentor = await applicant(db_engine, "note-bulk-long")

    response = await api_client.post(
        f"{ADMIN}/mentors/approvals",
        json={"user_ids": [str(mentor)], "message": "x" * (TEAM_NOTE_MAX_LENGTH + 1)},
        headers=bearer(token),
    )

    assert response.status_code == 422
    assert pointers(response) == ["/message"]
    assert await events(db_engine, mentor) == []

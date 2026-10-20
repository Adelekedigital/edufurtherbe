"""A declined mentor's booking setup is locked; their profile is not (owner, 2026-10-20).

Session types (with their windows and questions), hours, calendar connect,
conferencing and the mentor profile's booking rules answer `409`
`/problems/mentor-application-declined`. Reads, the rest of the profile and a
calendar disconnect stay open. A draft application is **not** locked: its own
onboarding writes hours, the Intro chat offering and the calendar.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor, token_for
from tests.integration.test_api_calendar_connection import (
    CALLBACK,
    KEY,
    FakeExchange,
    configured,
    wire,
)

from app.infra.clients.secrets import sealed_value
from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

PROBLEM = "/problems/mentor-application-declined"
RULE = {"day_of_week": 1, "start_time": "09:00", "end_time": "12:00", "timezone": "Africa/Lagos"}
EXCEPTION = {
    "type": "block",
    "start_date": "2027-03-01",
    "end_date": "2027-03-02",
    "timezone": "Africa/Lagos",
}
SOME = str(uuid4())

#: Every write the lock covers. `{me}` is the mentor's id; any other id may be
#: unknown, because the refusal comes before the lookup.
WRITES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "/api/v1/me/session-types", {"name": "Intro chat", "duration_minutes": 30}),
    ("patch", f"/api/v1/me/session-types/{SOME}", {"name": "Renamed"}),
    ("delete", f"/api/v1/me/session-types/{SOME}", None),
    ("post", f"/api/v1/me/session-types/{SOME}/restore", None),
    ("post", f"/api/v1/me/session-types/{SOME}/windows", RULE),
    ("patch", f"/api/v1/me/session-types/{SOME}/windows/{SOME}", {"is_active": False}),
    ("delete", f"/api/v1/me/session-types/{SOME}/windows/{SOME}", None),
    ("post", f"/api/v1/me/session-types/{SOME}/questions", {"question_text": "Why?"}),
    ("patch", f"/api/v1/me/session-types/{SOME}/questions/{SOME}", {"question_text": "How?"}),
    ("delete", f"/api/v1/me/session-types/{SOME}/questions/{SOME}", None),
    ("put", f"/api/v1/me/session-types/{SOME}/questions/order", {"question_ids": [SOME]}),
    ("post", "/api/v1/users/{me}/availability/rules", RULE),
    ("patch", "/api/v1/users/{me}/availability/rules/" + SOME, {"is_active": False}),
    ("delete", "/api/v1/users/{me}/availability/rules/" + SOME, None),
    ("post", "/api/v1/users/{me}/availability/exceptions", EXCEPTION),
    ("delete", "/api/v1/users/{me}/availability/exceptions/" + SOME, None),
    ("patch", "/api/v1/me/conferencing", {"provider": "daily"}),
    ("patch", "/api/v1/users/{me}/mentor-profile", {"booking_window_days": 30}),
]


async def a_mentor(engine: AsyncEngine, tag: str, *, status: str) -> tuple[UUID, str]:
    mentor = await make_public_mentor(engine, tag)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET approval_status = :s WHERE user_id = :u"),
            {"s": status, "u": mentor},
        )
    return mentor, await token_for(engine, mentor)


async def send(
    client: httpx.AsyncClient, method: str, url: str, body: dict[str, Any] | None, token: str
) -> httpx.Response:
    return await client.request(method, url, json=body, headers=bearer(token))


@pytest.mark.parametrize(("method", "url", "body"), WRITES, ids=[f"{m} {u}" for m, u, _ in WRITES])
async def test_each_booking_setup_write_is_refused_for_a_declined_mentor(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    url: str,
    body: dict[str, Any] | None,
) -> None:
    mentor, token = await a_mentor(db_engine, f"declined-{uuid4().hex[:8]}", status="declined")

    response = await send(api_client, method, url.format(me=mentor), body, token)

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith(PROBLEM)


@pytest.mark.parametrize(("method", "url", "body"), WRITES, ids=[f"{m} {u}" for m, u, _ in WRITES])
async def test_the_same_writes_are_open_to_an_approved_mentor(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    url: str,
    body: dict[str, Any] | None,
) -> None:
    mentor, token = await a_mentor(db_engine, f"approved-{uuid4().hex[:8]}", status="approved")

    response = await send(api_client, method, url.format(me=mentor), body, token)

    assert response.status_code != 409 or not response.json()["type"].endswith(PROBLEM)


async def test_a_draft_application_can_still_set_up_bookings_during_onboarding(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Onboarding's hours step and its Intro chat run before submit."""
    mentor, token = await a_mentor(db_engine, "declined-draft", status="pending")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET submitted_at = NULL WHERE user_id = :u"),
            {"u": mentor},
        )

    offering = await send(
        api_client,
        "post",
        "/api/v1/me/session-types",
        {"name": "Intro chat", "duration_minutes": 30},
        token,
    )
    hours = await send(
        api_client, "post", f"/api/v1/users/{mentor}/availability/rules", RULE, token
    )

    assert (offering.status_code, hours.status_code) == (201, 201), (offering.text, hours.text)


async def test_a_declined_mentor_still_edits_their_profile_and_reads_their_setup(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, token = await a_mentor(db_engine, "declined-profile", status="declined")

    edited = await send(
        api_client,
        "patch",
        f"/api/v1/users/{mentor}/mentor-profile",
        {"headline": "Reapplying with more detail"},
        token,
    )
    listed = await send(api_client, "get", "/api/v1/me/session-types", None, token)

    assert edited.status_code == 200, edited.text
    assert listed.status_code == 200, listed.text


async def test_the_refusal_names_the_booking_rules_the_profile_write_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, token = await a_mentor(db_engine, "declined-rules", status="declined")

    response = await send(
        api_client,
        "patch",
        f"/api/v1/users/{mentor}/mentor-profile",
        {"headline": "New", "default_duration_minutes": 45, "requires_booking_confirmation": True},
        token,
    )

    assert response.status_code == 409, response.text
    assert response.json()["fields"] == [
        "default_duration_minutes",
        "requires_booking_confirmation",
    ]
    async with db_engine.connect() as conn:
        headline = (
            await conn.execute(
                text("SELECT headline FROM mentor_profiles WHERE user_id = :u"), {"u": mentor}
            )
        ).scalar_one()
    assert headline != "New"


async def test_a_declined_mentor_cannot_start_or_finish_connecting_a_calendar(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A consent started before the decline stores nothing when it comes back."""
    mentor, token = await a_mentor(db_engine, "declined-calendar", status="declined")
    exchange = wire(api_client, settings=configured())
    state = sealed_value({"user_id": str(mentor)}, key=KEY)

    started = await send(api_client, "get", "/api/v1/me/calendar/connect", None, token)
    finished = await api_client.get(CALLBACK, params={"code": "the-code", "state": state})

    assert (started.status_code, finished.status_code) == (409, 409), (started.text, finished.text)
    assert isinstance(exchange, FakeExchange) and exchange.calls == []


async def test_a_declined_mentor_can_still_disconnect_a_calendar(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, token = await a_mentor(db_engine, "declined-disconnect", status="declined")

    response = await send(api_client, "delete", "/api/v1/me/calendar", None, token)

    # Nothing was connected: the route's own answer, not the lock's.
    assert response.status_code == 404, response.text


async def test_a_removed_mentor_profile_is_not_held_by_its_old_decline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The lock reads the live profile, as the review freeze does."""
    mentor, token = await a_mentor(db_engine, "declined-removed", status="declined")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )

    response = await send(
        api_client, "post", f"/api/v1/users/{mentor}/availability/rules", RULE, token
    )

    assert response.status_code != 409 or not response.json()["type"].endswith(PROBLEM)


async def test_booking_rules_resent_unchanged_beside_a_profile_edit_save(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A settings form resending every field: only a change is refused."""
    mentor, token = await a_mentor(db_engine, "declined-resend", status="declined")
    async with db_engine.connect() as conn:
        held = (
            (
                await conn.execute(
                    text(
                        "SELECT requires_booking_confirmation, booking_window_days, "
                        "default_duration_minutes FROM mentor_profiles WHERE user_id = :u"
                    ),
                    {"u": mentor},
                )
            )
            .mappings()
            .one()
        )

    response = await send(
        api_client,
        "patch",
        f"/api/v1/users/{mentor}/mentor-profile",
        {"headline": "Reapplying", **{k: v for k, v in held.items() if v is not None}},
        token,
    )

    assert response.status_code == 200, response.text


async def test_an_approved_mentor_sets_up_bookings_as_before(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive half of the lock: the same writes succeed when approved."""
    mentor, token = await a_mentor(db_engine, "approved-setup", status="approved")
    wire(api_client, settings=configured())

    answers = [
        (
            await send(
                api_client,
                "post",
                "/api/v1/me/session-types",
                {"name": "Intro chat", "duration_minutes": 30},
                token,
            )
        ).status_code,
        (
            await send(
                api_client, "post", f"/api/v1/users/{mentor}/availability/rules", RULE, token
            )
        ).status_code,
        (
            await send(
                api_client,
                "post",
                f"/api/v1/users/{mentor}/availability/exceptions",
                EXCEPTION,
                token,
            )
        ).status_code,
        (
            await send(api_client, "patch", "/api/v1/me/conferencing", {"provider": "daily"}, token)
        ).status_code,
        (
            await send(
                api_client,
                "patch",
                f"/api/v1/users/{mentor}/mentor-profile",
                {"booking_window_days": 30},
                token,
            )
        ).status_code,
        (await send(api_client, "get", "/api/v1/me/calendar/connect", None, token)).status_code,
    ]

    assert answers == [201, 201, 201, 200, 200, 200]

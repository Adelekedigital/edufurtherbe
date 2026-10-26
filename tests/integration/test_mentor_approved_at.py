"""When the current approval was granted, on the owner's own profile.

Mentor Home's "Listed on Explore · Since Oct 18" (FE request, 2026-10-25).
`approved_at` is the newest `approved` event, **only while approved**, by the
same rule `reviewed_at` reads the newest decline: a past approval says nothing
about a mentor who has since been declined. Owner only, like `approval_status`,
so absent for everybody else and absent when there is no date to give.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor, make_public_mentor, token_for
from tests.integration.test_mentor_owner_view import a_stranger

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def decided(engine: AsyncEngine, mentor: UUID, status_type: str, at: str) -> None:
    """A decision at `at`; the trigger projects it onto `approval_status`."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_status_events (mentor_user_id, status_type, created_at) "
                "VALUES (:u, :t, :at)"
            ),
            {"u": mentor, "t": status_type, "at": dt.datetime.fromisoformat(at)},
        )


async def own_view(client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID) -> dict:  # type: ignore[type-arg]
    response = await client.get(
        f"/api/v1/mentors/{mentor}", headers=bearer(await token_for(engine, mentor))
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_the_owner_reads_when_they_were_approved(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "approved-at")
    await decided(db_engine, mentor, "approved", "2026-10-18T09:30:00Z")

    body = await own_view(api_client, db_engine, mentor)

    assert body["approval_status"] == "approved"
    assert body["approved_at"].startswith("2026-10-18T09:30:00")


async def test_a_reapproval_is_dated_by_the_newest_approval(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "approved-at-again")
    await decided(db_engine, mentor, "approved", "2026-09-01T10:00:00Z")
    await decided(db_engine, mentor, "declined", "2026-09-15T10:00:00Z")
    await decided(db_engine, mentor, "approved", "2026-10-18T10:00:00Z")

    body = await own_view(api_client, db_engine, mentor)

    assert body["approved_at"].startswith("2026-10-18T10:00:00")


async def test_a_mentor_declined_after_an_approval_has_no_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "approved-at-declined")
    await decided(db_engine, mentor, "approved", "2026-09-01T10:00:00Z")
    await decided(db_engine, mentor, "declined", "2026-10-01T10:00:00Z")

    body = await own_view(api_client, db_engine, mentor)

    assert body["approval_status"] == "declined"
    assert "approved_at" not in body


async def test_a_pending_owner_has_no_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "approved-at-pending", approved=False)

    body = await own_view(api_client, db_engine, mentor)

    assert body["approval_status"] == "pending"
    assert "approved_at" not in body


async def test_an_approval_with_no_decision_row_has_no_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Imported mentors were approved with no event; the UI shows "Listed"."""
    mentor = await make_bookable_mentor(db_engine, "approved-at-legacy")

    body = await own_view(api_client, db_engine, mentor)

    assert body["approval_status"] == "approved"
    assert "approved_at" not in body


async def test_nobody_but_the_owner_reads_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "approved-at-private")
    await decided(db_engine, mentor, "approved", "2026-10-18T09:30:00Z")

    stranger = await api_client.get(
        f"/api/v1/mentors/{mentor}", headers=await a_stranger(db_engine, "approved-at")
    )
    anonymous = await api_client.get(f"/api/v1/mentors/{mentor}")

    for response in (stranger, anonymous):
        assert response.status_code == 200, response.text
        assert "approved_at" not in response.json()

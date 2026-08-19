"""Narrowing a mentor's status history by kind and by date.

**A log you can only read whole is a log nobody reads past the first page.** A
reviewer asking *"why was this mentor unlisted in March"* is filtering both at
once, so the two compose rather than being alternatives.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: Fixed rather than relative to now, so a range in a test names the instants it
#: means. Nothing here depends on the clock.
MARCH = dt.datetime(2026, 3, 10, 12, 0, tzinfo=dt.UTC)
APRIL = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.UTC)
MAY = dt.datetime(2026, 5, 10, 12, 0, tzinfo=dt.UTC)


async def a_reviewed_mentor(engine: AsyncEngine, tag: str) -> dict[str, Any]:
    """A mentor with one event of each kind, one a month apart."""
    mentor = await make_public_mentor(engine, tag)
    admin_auth = uuid4()
    async with engine.begin() as conn:
        admin = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, primary_role, timezone) "
                    "VALUES (:e, :a, 'mentor', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"admin-{tag}@example.test", "a": admin_auth},
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": admin},
        )
        # Written directly: `apply_mentor_status` stamps `now()`, and these need
        # to sit at named instants for a range to mean anything.
        for kind, when in (("approved", MARCH), ("listed", APRIL), ("unlisted", MAY)):
            await conn.execute(
                text(
                    "INSERT INTO mentor_status_events "
                    "(mentor_user_id, status_type, reason, created_at, created_by) "
                    "VALUES (:m, :k, :r, :t, :b)"
                ),
                {"m": mentor, "k": kind, "r": f"{kind} in test", "t": when, "b": admin},
            )
    return {"mentor": mentor, "headers": bearer(api_token(admin_auth))}


async def history(
    client: httpx.AsyncClient, setup: dict[str, Any], **params: Any
) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/admin/mentors/{setup['mentor']}/history",
        params=params,
        headers=setup["headers"],
    )
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


def kinds(rows: list[dict[str, Any]]) -> list[str]:
    return [row["status_type"] for row in rows]


# --------------------------------------------------------------------------
# By kind
# --------------------------------------------------------------------------


async def test_no_status_means_every_kind(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Omitting the filter means everything, not nothing.** The opposite
    reading is what an empty list from a client means, and the two must not be
    the same value."""
    setup = await a_reviewed_mentor(db_engine, "hf-all")

    assert kinds(await history(api_client, setup)) == ["unlisted", "listed", "approved"]


async def test_one_status_narrows_to_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup = await a_reviewed_mentor(db_engine, "hf-one")

    assert kinds(await history(api_client, setup, status="approved")) == ["approved"]


async def test_status_repeats_to_widen(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`?status=listed&status=unlisted` — the listing dimension on its own,
    which is the question *"has this mentor been in and out of the directory"*
    and cannot be asked one value at a time."""
    setup = await a_reviewed_mentor(db_engine, "hf-many")

    rows = await history(api_client, setup, status=["listed", "unlisted"])

    assert kinds(rows) == ["unlisted", "listed"]


async def test_an_unknown_status_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A closed vocabulary at the boundary. Silently ignoring it would answer a
    question nobody asked with a full log that looks filtered."""
    setup = await a_reviewed_mentor(db_engine, "hf-bogus")

    response = await api_client.get(
        f"/api/v1/admin/mentors/{setup['mentor']}/history",
        params={"status": "banished"},
        headers=setup["headers"],
    )

    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# By date
# --------------------------------------------------------------------------


async def test_since_is_inclusive_and_until_is_exclusive(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Half-open, so two adjacent ranges partition the log.** A closed upper
    bound would return an event landing exactly on the boundary in both, and a
    reviewer paging month by month would count it twice."""
    setup = await a_reviewed_mentor(db_engine, "hf-window")

    rows = await history(api_client, setup, since=MARCH.isoformat(), until=MAY.isoformat())

    assert kinds(rows) == ["listed", "approved"], "MAY is excluded, MARCH included"


async def test_the_two_halves_of_a_split_do_not_overlap(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The property the boundary rule exists for, asserted as a property rather
    than as two examples: every event appears in exactly one half."""
    setup = await a_reviewed_mentor(db_engine, "hf-partition")
    boundary = APRIL

    before = await history(api_client, setup, until=boundary.isoformat())
    after = await history(api_client, setup, since=boundary.isoformat())

    assert sorted(kinds(before) + kinds(after)) == ["approved", "listed", "unlisted"]
    assert set(kinds(before)) & set(kinds(after)) == set()


async def test_the_filters_compose(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    """The question that motivated this: *why was this mentor unlisted, and
    when*. Neither filter alone answers it on a mentor with a long history."""
    setup = await a_reviewed_mentor(db_engine, "hf-both")

    rows = await history(api_client, setup, status="unlisted", since=APRIL.isoformat())

    assert kinds(rows) == ["unlisted"]


async def test_a_backwards_range_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Returning nothing would be indistinguishable from a mentor with no
    events in the window, and a reviewer would read it as an answer."""
    setup = await a_reviewed_mentor(db_engine, "hf-backwards")

    response = await api_client.get(
        f"/api/v1/admin/mentors/{setup['mentor']}/history",
        params={"since": MAY.isoformat(), "until": MARCH.isoformat()},
        headers=setup["headers"],
    )

    assert response.status_code == 422, response.text


async def test_a_timezone_less_instant_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Refused rather than guessed at.** This log has no owning mentor whose
    day it could mean, and the reviewer's own zone is not something the server
    knows — the same rule `starts_at` follows on a booking."""
    setup = await a_reviewed_mentor(db_engine, "hf-naive")

    response = await api_client.get(
        f"/api/v1/admin/mentors/{setup['mentor']}/history",
        params={"since": "2026-03-10T12:00:00"},
        headers=setup["headers"],
    )

    assert response.status_code == 422, response.text


async def test_filtering_is_still_an_admin_surface(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The filters change what is returned, never who may ask. A non-admin gets
    the same `404` they always did — never `403`, which would confirm the
    endpoint exists and that somebody may use it."""
    setup = await a_reviewed_mentor(db_engine, "hf-scope")
    outsider = uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, primary_role, timezone) "
                "VALUES (:e, :a, 'mentee', 'Africa/Lagos')"
            ),
            {"e": f"outsider-{outsider}@example.test", "a": outsider},
        )

    response = await api_client.get(
        f"/api/v1/admin/mentors/{setup['mentor']}/history",
        params={"status": "approved"},
        headers=bearer(api_token(outsider)),
    )

    assert response.status_code == 404, response.text

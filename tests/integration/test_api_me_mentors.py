"""`GET /me/mentors` — the mentors a mentee has met (decision 248).

**Two mentees and several mentors wherever a scope is under test**, for the
reason `test_api_me_mentor_stats` gives: with one of each, "this mentee's
sessions" and "every session" are the same set and the scope could be deleted
with every assertion green.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_education,
    add_session,
    add_session_type,
    make_bookable_mentor,
    make_public_mentor,
    mentee_of,
    token_for,
)

from app.api.deps import PRIVATE_NO_STORE
from conftest import add_user, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/mentors"


async def page(
    client: httpx.AsyncClient, token: str, query: str = ""
) -> tuple[list[dict[str, Any]], str | None]:
    response = await client.get(URL + query, headers=bearer(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "next_cursor"}
    return body["data"], body["next_cursor"]


async def ids(client: httpx.AsyncClient, token: str, query: str = "") -> list[UUID]:
    rows, _ = await page(client, token, query)
    return [UUID(row["id"]) for row in rows]


async def met(engine: AsyncEngine, mentor: UUID, mentee: UUID | None = None, **knobs: Any) -> UUID:
    """A completed session with ``mentor``; returns the mentee (new unless given)."""
    session_id = await add_session(engine, mentor, mentee=mentee, **knobs)
    return mentee or await mentee_of(engine, session_id)


async def delete(engine: AsyncEngine, table: str, column: str, user: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE {table} SET deleted_at = now() WHERE {column} = :u"),  # noqa: S608
            {"u": user},
        )


async def bookability(client: httpx.AsyncClient, token: str) -> dict[UUID, str]:
    rows, _ = await page(client, token)
    return {UUID(row["id"]): row["bookability"] for row in rows}


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------


async def test_each_mentor_met_is_listed_with_their_card(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "met-card", slug="ada-met")
    await add_education(db_engine, mentor, level="doctorate", abbreviation="PhD", school="WashU")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles "
                "(user_id, avatar_url, avatar_focus_x, avatar_focus_y, avatar_focus_source) "
                "VALUES (:u, 'https://example.test/ada.jpg', 0.25, 0.75, 'detected')"
            ),
            {"u": mentor},
        )
    mentee = await met(db_engine, mentor, starts_at=dt.datetime(2025, 12, 1, 9, tzinfo=dt.UTC))
    latest = dt.datetime(2026, 1, 2, 9, 30, tzinfo=dt.UTC)
    await met(db_engine, mentor, mentee, starts_at=latest)

    rows, cursor = await page(api_client, await token_for(db_engine, mentee))

    assert rows == [
        {
            "id": str(mentor),
            "slug": "ada-met",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "avatar_url": "https://example.test/ada.jpg",
            "avatar_focus": {"x": 0.25, "y": 0.75},
            "cover_color": None,
            "cover_art": "none",
            "degree": "PhD",
            "institution": "WashU",
            "session_count": 2,
            "last_session_at": "2026-01-02T09:30:00Z",
            "bookability": "open",
            "next_session_at": None,
            "unavailable_reason": None,
        }
    ]
    assert cursor is None


async def test_the_response_is_never_cached_by_anyone_but_the_caller(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "met-cache")
    mentee = await met(db_engine, mentor)

    response = await api_client.get(URL, headers=bearer(await token_for(db_engine, mentee)))

    assert response.headers["cache-control"] == PRIVATE_NO_STORE


async def test_a_token_is_required(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(URL)).status_code == 401


# --------------------------------------------------------------------------
# Who counts as met
# --------------------------------------------------------------------------


async def test_another_mentees_mentors_and_sessions_never_appear(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mine = await make_bookable_mentor(db_engine, "met-mine")
    theirs = await make_bookable_mentor(db_engine, "met-theirs")
    me = await met(db_engine, mine, days_ago=3)
    other = await met(db_engine, theirs, days_ago=2)
    # The other mentee also met my mentor, more recently and twice.
    await met(db_engine, mine, other, days_ago=1)
    await met(db_engine, mine, other, days_ago=4)

    rows, _ = await page(api_client, await token_for(db_engine, me))

    assert [UUID(row["id"]) for row in rows] == [mine]
    assert rows[0]["session_count"] == 1
    assert await ids(api_client, await token_for(db_engine, other)) == [mine, theirs]


async def test_only_completed_sessions_count_as_meeting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor met only in sessions that did not happen is not listed, and
    those sessions neither count nor move `last_session_at`."""
    held = await make_bookable_mentor(db_engine, "met-held")
    never = await make_bookable_mentor(db_engine, "met-never")
    first = dt.datetime(2026, 3, 1, 10, tzinfo=dt.UTC)
    mentee = await met(db_engine, held, starts_at=first)
    await met(db_engine, held, mentee, status="cancelled", days_ago=2)
    for days_ago, status in enumerate(
        ("cancelled", "no_show", "declined", "expired", "withdrawn"), start=3
    ):
        await met(db_engine, never, mentee, status=status, days_ago=days_ago)

    rows, _ = await page(api_client, await token_for(db_engine, mentee))

    assert [UUID(row["id"]) for row in rows] == [held]
    assert rows[0]["session_count"] == 1
    assert rows[0]["last_session_at"] == "2026-03-01T10:00:00Z"


async def test_a_deleted_mentor_is_not_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Gone from this list whether the account or the mentor profile was
    deleted; a live mentor met the same way stays (owner, 2026-10-14)."""
    live = await make_bookable_mentor(db_engine, "met-live")
    left = await make_bookable_mentor(db_engine, "met-left")
    retired = await make_bookable_mentor(db_engine, "met-retired")
    mentee = await met(db_engine, live, days_ago=3)
    await met(db_engine, left, mentee, days_ago=2)
    await met(db_engine, retired, mentee, days_ago=1)
    token = await token_for(db_engine, mentee)
    assert await ids(api_client, token) == [retired, left, live]

    await delete(db_engine, "users", "id", left)
    await delete(db_engine, "mentor_profiles", "user_id", retired)

    assert await ids(api_client, token) == [live]


async def test_somebody_who_met_nobody_reads_an_empty_page(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor who has only mentored, and so has received nothing."""
    mentor = await make_bookable_mentor(db_engine, "met-only-mentor")
    await met(db_engine, mentor)

    assert await page(api_client, await token_for(db_engine, mentor)) == ([], None)


# --------------------------------------------------------------------------
# Slug
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("approved", "listed", "published"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_the_slug_is_published_only_while_the_mentor_is_live(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    approved: bool,
    listed: bool,
    published: bool,
) -> None:
    mentor = await make_public_mentor(
        db_engine, "met-slug", approved=approved, listed=listed, slug="ada-slug"
    )
    mentee = await met(db_engine, mentor)

    rows, _ = await page(api_client, await token_for(db_engine, mentee))

    assert rows[0]["slug"] == ("ada-slug" if published else None)


# --------------------------------------------------------------------------
# Bookability
# --------------------------------------------------------------------------


async def test_each_bookability_state(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    upcoming = await make_bookable_mentor(db_engine, "met-upcoming")
    pending = await make_bookable_mentor(db_engine, "met-pending")
    unlisted = await make_bookable_mentor(db_engine, "met-unlisted", listed=False)
    no_hours = await make_public_mentor(db_engine, "met-no-hours")
    await add_session_type(db_engine, no_hours)
    open_ = await make_bookable_mentor(db_engine, "met-open")
    mentee = await met(db_engine, open_)
    for mentor in (upcoming, pending, unlisted, no_hours):
        await met(db_engine, mentor, mentee, days_ago=5)
    await add_session(db_engine, upcoming, mentee=mentee, status="confirmed", days_ago=-2)
    await add_session(
        db_engine, pending, mentee=mentee, status="pending_mentor_approval", days_ago=-3
    )

    assert await bookability(api_client, await token_for(db_engine, mentee)) == {
        upcoming: "has_upcoming",
        pending: "pending",
        unlisted: "unavailable",
        no_hours: "unavailable",
        open_: "open",
    }


async def test_has_upcoming_outranks_pending_and_both_outrank_unavailable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    both = await make_bookable_mentor(db_engine, "met-both")
    away_upcoming = await make_bookable_mentor(db_engine, "met-away-up", listed=False)
    away_pending = await make_bookable_mentor(db_engine, "met-away-pend", listed=False)
    mentee = await met(db_engine, both, days_ago=5)
    await met(db_engine, away_upcoming, mentee, days_ago=6)
    await met(db_engine, away_pending, mentee, days_ago=7)
    await add_session(db_engine, both, mentee=mentee, status="confirmed", days_ago=-2)
    await add_session(db_engine, both, mentee=mentee, status="pending_mentor_approval", days_ago=-3)
    await add_session(db_engine, away_upcoming, mentee=mentee, status="confirmed", days_ago=-4)
    await add_session(
        db_engine, away_pending, mentee=mentee, status="pending_mentor_approval", days_ago=-5
    )

    assert await bookability(api_client, await token_for(db_engine, mentee)) == {
        both: "has_upcoming",
        away_upcoming: "has_upcoming",
        away_pending: "pending",
    }


async def test_only_the_callers_own_live_sessions_make_a_mentor_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Another mentee's upcoming session or request with the same mentor is
    not the caller's; nor is a session that has started, or a request past
    its deadline."""
    shared = await make_bookable_mentor(db_engine, "met-shared")
    stale = await make_bookable_mentor(db_engine, "met-stale")
    mentee = await met(db_engine, shared, days_ago=5)
    await met(db_engine, stale, mentee, days_ago=6)
    other = await met(db_engine, shared, days_ago=7)
    await add_session(db_engine, shared, mentee=other, status="confirmed", days_ago=-2)
    await add_session(
        db_engine, shared, mentee=other, status="pending_mentor_approval", days_ago=-3
    )
    # Started an hour ago and still confirmed: not upcoming. A request whose
    # session started an hour ago has lapsed: not pending.
    hour_ago = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    await add_session(db_engine, stale, mentee=mentee, status="confirmed", starts_at=hour_ago)
    await add_session(
        db_engine,
        stale,
        mentee=mentee,
        status="pending_mentor_approval",
        starts_at=hour_ago - dt.timedelta(hours=2),
    )

    assert await bookability(api_client, await token_for(db_engine, mentee)) == {
        shared: "open",
        stale: "open",
    }
    assert await bookability(api_client, await token_for(db_engine, other)) == {
        shared: "has_upcoming",
    }


# --------------------------------------------------------------------------
# Order and pages
# --------------------------------------------------------------------------


async def test_most_recent_session_first_and_every_mentor_once_across_pages(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two mentors share a `last_session_at`, so the id breaks the tie and a
    page boundary between them neither skips nor repeats one."""
    mentors = [await make_bookable_mentor(db_engine, f"met-page-{n}") for n in range(4)]
    tied = dt.datetime(2026, 5, 1, 12, tzinfo=dt.UTC)
    mentee = await met(db_engine, mentors[0], starts_at=tied - dt.timedelta(days=9))
    await met(db_engine, mentors[1], mentee, starts_at=tied)
    await met(db_engine, mentors[2], mentee, starts_at=tied)
    await met(db_engine, mentors[3], mentee, starts_at=tied + dt.timedelta(days=1))
    token = await token_for(db_engine, mentee)
    expected = [
        mentors[3],
        *sorted((mentors[1], mentors[2]), reverse=True),
        mentors[0],
    ]

    assert await ids(api_client, token) == expected
    first, cursor = await page(api_client, token, "?limit=2")
    assert [UUID(row["id"]) for row in first] == expected[:2]
    assert cursor is not None
    second, last = await page(api_client, token, f"?limit=2&cursor={cursor}")
    assert [UUID(row["id"]) for row in second] == expected[2:]
    assert last is None


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        base64.urlsafe_b64encode(b"yesterday\x0000000000-0000-0000-0000-000000000000").decode(),
    ],
)
async def test_a_cursor_this_endpoint_did_not_issue_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, cursor: str
) -> None:
    mentor = await make_bookable_mentor(db_engine, "met-cursor")
    mentee = await met(db_engine, mentor)

    response = await api_client.get(
        f"{URL}?cursor={cursor}", headers=bearer(await token_for(db_engine, mentee))
    )

    assert response.status_code == 422, response.text


async def test_a_mentor_with_no_mentor_profile_is_listed_as_unavailable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Not deleted, so not dropped: a migrated mentor may have no profile row,
    and the sessions still happened. Nobody can book them."""
    async with db_engine.begin() as conn:
        bare = await add_user(
            conn,
            "bare-mentor@example.test",
            first_name="Bare",
            primary_role="mentor",
            timezone="UTC",
        )
    mentee = await met(db_engine, bare)

    assert await bookability(api_client, await token_for(db_engine, mentee)) == {
        bare: "unavailable"
    }


async def test_a_session_in_progress_counts_as_upcoming(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Started ten minutes ago and not yet over: the mentee is still booked
    with that mentor, so `open` would invite a second booking."""
    mentor = await make_bookable_mentor(db_engine, "met-in-progress")
    mentee = await met(db_engine, mentor, days_ago=5)
    started = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    await add_session(db_engine, mentor, mentee=mentee, status="confirmed", starts_at=started)

    assert await bookability(api_client, await token_for(db_engine, mentee)) == {
        mentor: "has_upcoming"
    }


# --------------------------------------------------------------------------
# Mentee Home's "Next on Thu, Oct 15" and "No open times yet"
# --------------------------------------------------------------------------


async def cards(client: httpx.AsyncClient, token: str) -> dict[UUID, dict[str, Any]]:
    rows, _ = await page(client, token)
    return {UUID(row["id"]): row for row in rows}


async def cache_next_time(engine: AsyncEngine, mentor: UUID, at: dt.datetime | None) -> None:
    """A vouched refresh-job row: a free time at ``at``, or none in the window."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_next_availability "
                "(mentor_user_id, next_available_at, bookable_until, computed_at) "
                "VALUES (:u, :at, :until, now())"
            ),
            {"u": mentor, "at": at, "until": at - dt.timedelta(hours=1) if at else None},
        )
        await conn.execute(
            text("DELETE FROM mentor_availability_changes WHERE mentor_user_id = :u"),
            {"u": mentor},
        )


async def test_next_session_is_the_earliest_upcoming_or_pending_booking(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The caller's own: another mentee's earlier booking with the same mentor
    is not theirs, and a finished session is not next."""
    mentor = await make_bookable_mentor(db_engine, "met-next")
    quiet = await make_bookable_mentor(db_engine, "met-next-quiet")
    mentee = await met(db_engine, mentor, days_ago=5)
    await met(db_engine, quiet, mentee, days_ago=6)
    other = await met(db_engine, mentor, days_ago=7)
    soon = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(days=2)
    await add_session(
        db_engine, mentor, mentee=mentee, status="pending_mentor_approval", starts_at=soon
    )
    await add_session(
        db_engine, mentor, mentee=mentee, status="confirmed", starts_at=soon + dt.timedelta(days=3)
    )
    await add_session(
        db_engine, mentor, mentee=other, status="confirmed", starts_at=soon - dt.timedelta(days=1)
    )

    mine = await cards(api_client, await token_for(db_engine, mentee))

    assert dt.datetime.fromisoformat(mine[mentor]["next_session_at"]) == soon
    assert mine[quiet]["next_session_at"] is None


async def test_why_a_mentor_cannot_be_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`not_taking` exactly when `unavailable`; `no_open_times` for a mentor
    who takes bookings but has nothing free in their window; null otherwise,
    including a mentor the refresh job has not reached yet."""
    away = await make_bookable_mentor(db_engine, "met-why-away", listed=False)
    full = await make_bookable_mentor(db_engine, "met-why-full")
    free = await make_bookable_mentor(db_engine, "met-why-free")
    unknown = await make_bookable_mentor(db_engine, "met-why-unknown")
    booked = await make_bookable_mentor(db_engine, "met-why-booked")
    mentee = await met(db_engine, away, days_ago=5)
    for mentor in (full, free, unknown, booked):
        await met(db_engine, mentor, mentee, days_ago=6)
    await cache_next_time(db_engine, full, None)
    await cache_next_time(db_engine, free, dt.datetime.now(dt.UTC) + dt.timedelta(days=3))
    await cache_next_time(db_engine, booked, None)
    await add_session(db_engine, booked, mentee=mentee, status="confirmed", days_ago=-2)

    mine = await cards(api_client, await token_for(db_engine, mentee))

    assert {m: mine[m]["unavailable_reason"] for m in mine} == {
        away: "not_taking",
        full: "no_open_times",
        free: None,
        unknown: None,
        booked: None,
    }
    assert mine[full]["bookability"] == "open"

"""`GET /me/mentor-stats` — a mentor's own figures over a window (decision 246).

**Two mentors wherever a scope is under test**, for the reason
`test_api_me_session_types` gives: with one mentor in the database "this
mentor's rows" and "all rows" are the same set, and the scope could be deleted
with every assertion green. And fixtures are distinguishable: different
durations, a repeated mentee, so a wrong formula gives a different number.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_session, make_bookable_mentor

from app.domain.mentor_stats import Window
from app.infra.db.mentor_dashboard import mentor_window_stats
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/mentor-stats"

ZEROS = {
    "completed_sessions": 0,
    "mentoring_minutes": 0,
    "mentees_mentored": 0,
    "attendance_rate": None,
    "missed_by_mentor": 0,
    "cancelled_by_mentor": 0,
    "review_count": 0,
    "session_value": None,
}


async def token_for(engine: AsyncEngine, user: UUID) -> str:
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": user}
        )
    return api_token(auth_id)


async def stats(
    client: httpx.AsyncClient, token: str, query: str = "?window_days=30"
) -> dict[str, Any]:
    response = await client.get(URL + query, headers=bearer(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def figures(block: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if key not in ("since", "until")}


async def mentee_of(engine: AsyncEngine, session_id: UUID) -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text("SELECT mentee_id FROM sessions WHERE id = :s"), {"s": session_id}
                    )
                ).scalar_one()
            )
        )


async def ended_by(engine: AsyncEngine, session_id: UUID, actor: UUID | None, to: str) -> None:
    """The event a transition writes: who moved the session to ``to``."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO session_events (session_id, from_status, to_status, actor_id, "
                "actor_type) VALUES (:s, 'confirmed', :to, :a, :t)"
            ),
            {"s": session_id, "to": to, "a": actor, "t": "user" if actor else "system"},
        )


async def cancelled(engine: AsyncEngine, mentor: UUID, by: str, *, days_ago: int = 3) -> UUID:
    """A cancelled session, cancelled by `mentor`, `mentee`, or nobody named."""
    session_id = await add_session(
        engine, mentor, status="cancelled", attendance="pending", days_ago=days_ago
    )
    actor = {"mentor": mentor, "mentee": await mentee_of(engine, session_id), "system": None}[by]
    await ended_by(engine, session_id, actor, "cancelled")
    return session_id


async def review(
    engine: AsyncEngine,
    mentor: UUID,
    *,
    overall: int | None,
    valuable: int = 3,
    written: dt.datetime | None = None,
    withdrawn: bool = False,
) -> None:
    async with engine.begin() as conn:
        author = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES (:e, 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"author-{uuid4()}@example.test"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO reviews (reviewed_by, reviewed_for, communication_rating, "
                "knowledge_rating, practicality_rating, support_rating, valuable_rating, "
                "overall_rating, nps_recommend_score, public_review, created_at, deleted_at) "
                "VALUES (:by, :m, 3, 3, 3, 3, :v, :o, 9, 'Fine.', "
                "        COALESCE(CAST(:w AS timestamptz), now() - interval '2 days'), "
                "        CASE WHEN :gone THEN now() ELSE NULL END)"
            ),
            {
                "by": author,
                "m": mentor,
                "v": valuable,
                "o": overall,
                "w": written,
                "gone": withdrawn,
            },
        )


# --------------------------------------------------------------------------
# The figures
# --------------------------------------------------------------------------


async def test_each_figure_is_counted_in_the_current_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "dash-figures")
    first = await add_session(db_engine, mentor, minutes=30, days_ago=2)
    repeat = await mentee_of(db_engine, first)
    await add_session(db_engine, mentor, minutes=45, mentee=repeat, days_ago=3)
    await add_session(db_engine, mentor, minutes=60, days_ago=4)
    await add_session(db_engine, mentor, status="no_show", attendance="no_show", days_ago=5)
    await cancelled(db_engine, mentor, "mentor")
    await review(db_engine, mentor, overall=None, valuable=3)
    await review(db_engine, mentor, overall=None, valuable=2)

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert body["window_days"] == 30
    assert figures(body["current"]) == {
        "completed_sessions": 3,
        "mentoring_minutes": 135,
        "mentees_mentored": 2,
        "attendance_rate": 75,
        "missed_by_mentor": 1,
        "cancelled_by_mentor": 1,
        "review_count": 2,
        "session_value": 2.5,
    }
    assert figures(body["previous"]) == ZEROS


async def test_the_previous_window_holds_the_period_before(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """45 days ago is in `previous` for a 30-day window; 90 days ago is in
    neither."""
    mentor = await make_bookable_mentor(db_engine, "dash-previous")
    await add_session(db_engine, mentor, minutes=50, days_ago=45)
    await add_session(db_engine, mentor, minutes=20, days_ago=90)
    await cancelled(db_engine, mentor, "mentor", days_ago=40)
    await review(
        db_engine, mentor, overall=4, written=dt.datetime.now(dt.UTC) - dt.timedelta(days=50)
    )

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert figures(body["current"]) == ZEROS
    assert figures(body["previous"]) == {
        "completed_sessions": 1,
        "mentoring_minutes": 50,
        "mentees_mentored": 1,
        "attendance_rate": 100,
        "missed_by_mentor": 0,
        "cancelled_by_mentor": 1,
        "review_count": 1,
        "session_value": 4.0,
    }


async def test_the_windows_are_adjacent_and_utc(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "dash-bounds")

    body = await stats(api_client, await token_for(db_engine, mentor), "?window_days=7")

    current, previous = body["current"], body["previous"]
    since, until = (dt.datetime.fromisoformat(current[k]) for k in ("since", "until"))
    assert until - since == dt.timedelta(days=7)
    assert previous["until"] == current["since"]
    assert until.utcoffset() == dt.timedelta(0)


async def test_only_the_callers_sessions_and_reviews_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Another mentor's sessions, cancellations and reviews never reach the
    caller's figures — and nor do sessions the caller received as a mentee."""
    mentor = await make_bookable_mentor(db_engine, "dash-mine")
    other = await make_bookable_mentor(db_engine, "dash-theirs")
    await add_session(db_engine, mentor, minutes=30, days_ago=2)
    await add_session(db_engine, other, minutes=90, days_ago=2)
    await add_session(db_engine, other, status="no_show", attendance="no_show", days_ago=3)
    await cancelled(db_engine, other, "mentor")
    await review(db_engine, other, overall=1)
    # The caller as somebody's mentee: their own attendance there is not hosting.
    await add_session(db_engine, other, minutes=15, mentee=mentor, days_ago=4)

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert figures(body["current"]) == {
        **ZEROS,
        "completed_sessions": 1,
        "mentoring_minutes": 30,
        "mentees_mentored": 1,
        "attendance_rate": 100,
    }


async def test_a_non_mentor_reads_zeros_not_an_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with db_engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES ('plain@example.test', 'mentee', 'UTC') RETURNING id"
                )
            )
        ).scalar_one()

    body = await stats(api_client, await token_for(db_engine, user), "")

    assert body["window_days"] == 90
    assert figures(body["current"]) == ZEROS
    assert figures(body["previous"]) == ZEROS


# --------------------------------------------------------------------------
# Who cancelled, and who missed
# --------------------------------------------------------------------------


async def test_only_the_mentors_own_cancellations_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Cancelled by the mentee, by the system, or with no event at all is not
    the mentor's; a request the mentor declined is not a cancellation."""
    mentor = await make_bookable_mentor(db_engine, "dash-cancel")
    await cancelled(db_engine, mentor, "mentor")
    await cancelled(db_engine, mentor, "mentee")
    await cancelled(db_engine, mentor, "system")
    await add_session(db_engine, mentor, status="cancelled", attendance="pending", days_ago=3)
    declined = await add_session(
        db_engine, mentor, status="declined", attendance="pending", days_ago=3
    )
    await ended_by(db_engine, declined, mentor, "declined")

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert body["current"]["cancelled_by_mentor"] == 1


async def test_only_the_cancel_event_names_the_canceller(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The mentor accepting a session that was later cancelled with no recorded
    canceller does not make it the mentor's cancellation."""
    mentor = await make_bookable_mentor(db_engine, "dash-accepted")
    session_id = await add_session(
        db_engine, mentor, status="cancelled", attendance="pending", days_ago=3
    )
    await ended_by(db_engine, session_id, mentor, "confirmed")

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert body["current"]["cancelled_by_mentor"] == 0


async def test_the_session_status_decides_it_was_cancelled(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Each figure reads the column it is about (decision 101): a session that
    says `completed` is held, whatever a contradicting event says."""
    mentor = await make_bookable_mentor(db_engine, "dash-contradiction")
    session_id = await add_session(db_engine, mentor, days_ago=3)
    await ended_by(db_engine, session_id, mentor, "cancelled")

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert body["current"]["cancelled_by_mentor"] == 0
    assert body["current"]["completed_sessions"] == 1


async def test_missed_counts_settled_sessions_the_mentor_did_not_attend(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A cancelled session is not missed, `pending` is unknown, and a session
    the mentor attended but the mentee missed is not the mentor's miss."""
    mentor = await make_bookable_mentor(db_engine, "dash-missed")
    await add_session(db_engine, mentor, status="no_show", attendance="no_show", days_ago=2)
    await add_session(
        db_engine, mentor, status="no_show", attendance="attended", mentee_attendance="no_show"
    )
    await add_session(db_engine, mentor, status="cancelled", attendance="no_show", days_ago=3)
    await add_session(db_engine, mentor, status="no_show", attendance="pending", days_ago=4)

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert body["current"]["missed_by_mentor"] == 1
    assert body["current"]["attendance_rate"] == 50


async def test_withdrawn_reviews_do_not_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "dash-withdrawn")
    await review(db_engine, mentor, overall=5)
    await review(db_engine, mentor, overall=1, withdrawn=True)

    body = await stats(api_client, await token_for(db_engine, mentor))

    assert body["current"]["review_count"] == 1
    assert body["current"]["session_value"] == 5.0


# --------------------------------------------------------------------------
# The edges, at the store, where the clock is ours
# --------------------------------------------------------------------------

NOW = dt.datetime(2026, 6, 15, 12, tzinfo=dt.UTC)
WINDOW = Window(since=NOW - dt.timedelta(days=7), until=NOW)


async def window_stats(engine: AsyncEngine, mentor: UUID) -> dict[str, Any]:
    async with async_sessionmaker(engine)() as session:
        return await mentor_window_stats(session, mentor, WINDOW)


async def test_since_is_inside_the_window(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "dash-since")
    await add_session(db_engine, mentor, minutes=40, starts_at=WINDOW.since)
    await add_session(
        db_engine, mentor, status="no_show", attendance="no_show", starts_at=WINDOW.since
    )
    cancel = await add_session(
        db_engine, mentor, status="cancelled", attendance="pending", starts_at=WINDOW.since
    )
    await ended_by(db_engine, cancel, mentor, "cancelled")
    await review(db_engine, mentor, overall=4, written=WINDOW.since)

    row = await window_stats(db_engine, mentor)

    assert (row["completed_sessions"], row["mentoring_minutes"]) == (1, 40)
    assert (row["missed_by_mentor"], row["attendance_rate"]) == (1, 50)
    assert (row["cancelled_by_mentor"], row["review_count"]) == (1, 1)


async def test_until_is_outside_the_window(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "dash-until")
    await add_session(db_engine, mentor, minutes=40, starts_at=WINDOW.until)
    await add_session(
        db_engine, mentor, status="no_show", attendance="no_show", starts_at=WINDOW.until
    )
    cancel = await add_session(
        db_engine, mentor, status="cancelled", attendance="pending", starts_at=WINDOW.until
    )
    await ended_by(db_engine, cancel, mentor, "cancelled")
    await review(db_engine, mentor, overall=4, written=WINDOW.until)

    row = await window_stats(db_engine, mentor)

    assert {key: row[key] for key in ZEROS} == ZEROS
    assert (row["since"], row["until"]) == WINDOW


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("days", "code"), [(6, 422), (7, 200), (365, 200), (366, 422)])
async def test_window_days_is_bounded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, days: int, code: int
) -> None:
    mentor = await make_bookable_mentor(db_engine, f"dash-days-{days}")

    response = await api_client.get(
        f"{URL}?window_days={days}", headers=bearer(await token_for(db_engine, mentor))
    )

    assert response.status_code == code, response.text


async def test_it_needs_a_token(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(URL)).status_code == 401


async def test_nobody_else_caches_it(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "dash-cache")

    response = await api_client.get(URL, headers=bearer(await token_for(db_engine, mentor)))

    assert response.headers["Cache-Control"] == "private, no-store"

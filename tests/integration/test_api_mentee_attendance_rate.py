"""How often a mentee turns up, on the row where a mentor decides.

**The null case is the one this file exists for.** `session_stats` got the
mentor-side rate right by asserting it explicitly, and the same mistake is
available here and worse: a mentee's *first* booking has no data, so rendering
null as `0%` would greet every new mentee with "never shows up" on the request
card their mentor is looking at.

The API returns null and says nothing else. Substituting the words would be a
display decision made in the wrong layer, in one language, that no client could
change — the rule `PartyRead` already follows for a party with no name.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_pair(engine: AsyncEngine, tag: str) -> dict[str, Any]:
    """A mentor and a mentee, and the headers for each."""
    mentor_auth, mentee_auth = uuid4(), uuid4()
    async with engine.begin() as conn:
        mentor = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentor', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentor-{tag}@example.test", "a": mentor_auth},
            )
        ).scalar_one()
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Mo', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.test", "a": mentee_auth},
            )
        ).scalar_one()
    return {
        "mentor": mentor,
        "mentee": mentee,
        "mentor_headers": bearer(api_token(mentor_auth)),
        "mentee_headers": bearer(api_token(mentee_auth)),
    }


async def add_session(
    engine: AsyncEngine,
    pair: dict[str, Any],
    *,
    status: str,
    mentee_attended: str | None,
    days_ago: int,
    mentor: UUID | None = None,
    mentee: UUID | None = None,
) -> UUID:
    """One session in a terminal-or-not state, with the mentee's attendance set.

    `mentee_attended` of ``None`` writes no participant row at all, which is the
    state two of the 105 dev bookings are in — a session with no tracker.
    """
    mentor_id = mentor or pair["mentor"]
    mentee_id = mentee or pair["mentee"]
    async with engine.begin() as conn:
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(mentor_id, mentee_id, starts_at, duration_minutes, status) "
                    "VALUES (:m, :e, :s, 45, :status) RETURNING id"
                ),
                {
                    "m": mentor_id,
                    "e": mentee_id,
                    # Spaced by the hour so nothing overlaps, in case a status
                    # this fixture writes is ever added to the live set.
                    "s": dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago, hours=days_ago),
                    "status": status,
                },
            )
        ).scalar_one()
        if mentee_attended is not None:
            await conn.execute(
                text(
                    "INSERT INTO session_participants "
                    "(session_id, user_id, role, attendance_status) "
                    "VALUES (:i, :u, 'mentee', :a)"
                ),
                {"i": session_id, "u": mentee_id, "a": mentee_attended},
            )
    return UUID(str(session_id))


async def rate(client: httpx.AsyncClient, pair: dict[str, Any], session_id: UUID) -> Any:
    response = await client.get(f"/api/v1/sessions/{session_id}", headers=pair["mentor_headers"])
    assert response.status_code == 200, response.text
    return response.json()["mentee_attendance_rate"]


# --------------------------------------------------------------------------
# The null case, first and on its own
# --------------------------------------------------------------------------


async def test_a_mentee_with_no_finished_sessions_has_no_rate(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**`null`, and a client renders it as "New mentee".**

    Their only session is the one being looked at, and it has not happened. Zero
    would say *never shows up* about somebody who has never had the chance —
    which is the exact figure a mentor would decline on.
    """
    pair = await a_pair(db_engine, "rate-new")
    upcoming = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, upcoming) is None


async def test_a_mentee_with_no_participant_row_has_no_rate(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two of the 105 dev bookings have no tracker and so no participant rows.

    *Unknown* must not enter a denominator, and the inner join is what keeps it
    out — an outer join would count the session as expected-and-absent and
    report a migrated mentee at 0%.
    """
    pair = await a_pair(db_engine, "rate-notracker")
    orphan = await add_session(
        db_engine, pair, status="completed", mentee_attended=None, days_ago=5
    )

    assert await rate(api_client, pair, orphan) is None


async def test_a_mentee_who_is_only_pending_has_no_rate(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`pending` is *unknown*, not absent. A finished session that has not been
    settled yet must not count as an absence, or the hour between a session
    ending and the sweep running would show every mentee at 0%."""
    pair = await a_pair(db_engine, "rate-pending")
    unsettled = await add_session(
        db_engine, pair, status="completed", mentee_attended="pending", days_ago=2
    )

    assert await rate(api_client, pair, unsettled) is None


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


async def test_a_mentee_who_always_turns_up_is_a_hundred(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "rate-perfect")
    for day in (2, 4):
        await add_session(
            db_engine, pair, status="completed", mentee_attended="attended", days_ago=day
        )
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, current) == 100


async def test_the_rate_is_a_rounded_percentage(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two of three is 67, not 66.67 and not 66.

    Rounded in SQL rather than in Python: `round()` returns numeric, so the
    value arrives as "67.0" and `int()` refuses it, and fixing that at the
    boundary would leave the column's type a lie for every other reader.
    """
    pair = await a_pair(db_engine, "rate-two-thirds")
    await add_session(db_engine, pair, status="no_show", mentee_attended="no_show", days_ago=2)
    for day in (4, 6):
        await add_session(
            db_engine, pair, status="completed", mentee_attended="attended", days_ago=day
        )
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, current) == 67


async def test_a_mentee_who_never_turns_up_is_zero_and_not_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The other half of the null rule, and the reason it is a rule.**

    Zero and null are different claims and both are reachable. If a mistake
    collapsed them, the tests above would still pass while a mentor lost the
    only signal that matters.
    """
    pair = await a_pair(db_engine, "rate-never")
    await add_session(db_engine, pair, status="no_show", mentee_attended="no_show", days_ago=2)
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, current) == 0


# --------------------------------------------------------------------------
# What the denominator excludes
# --------------------------------------------------------------------------


async def test_a_cancelled_session_is_not_a_missed_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Nobody was expected, so nobody was absent. Counting it would punish a
    mentee for a session the *mentor* called off."""
    pair = await a_pair(db_engine, "rate-cancelled")
    await add_session(db_engine, pair, status="completed", mentee_attended="attended", days_ago=2)
    await add_session(db_engine, pair, status="cancelled", mentee_attended="pending", days_ago=4)
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, current) == 100


async def test_a_session_still_ahead_is_not_in_the_denominator(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """It has not happened. Counting it would drop the rate of a mentee who has
    attended everything, the moment they book again."""
    pair = await a_pair(db_engine, "rate-upcoming")
    await add_session(db_engine, pair, status="completed", mentee_attended="attended", days_ago=2)
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, current) == 100


# --------------------------------------------------------------------------
# Which side of the table it counts
# --------------------------------------------------------------------------


async def test_a_mentees_own_hosting_record_does_not_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Dual roles are free by design, so this is reachable rather than
    theoretical.**

    A person who hosts diligently and books unreliably has two records, and
    pooling them would let the first flatter the second on the card where a
    mentor decides. This is the inversion the discovery card already tests for
    on `delivered()`, from the other side.

    **The subject has to be the same person on both sides**, which is the whole
    difficulty of building this state and the reason a first version of this
    test proved nothing: it gave the *hosting* record to the mentor rather than
    to the mentee, so the two populations never met and dropping the side clause
    changed no answer. A mutation is what found that.
    """
    pair = await a_pair(db_engine, "rate-dual")
    third = await a_pair(db_engine, "rate-dual-third")
    # As a mentee, on somebody else's session: absent, so 0%.
    await add_session(db_engine, pair, status="no_show", mentee_attended="no_show", days_ago=2)
    # The same person, now **hosting** — and present. The `mentor` participant
    # row is the one `MENTOR` counts and `MENTEE` must not.
    hosted = await add_session(
        db_engine,
        pair,
        status="completed",
        mentee_attended=None,
        days_ago=4,
        mentor=pair["mentee"],
        mentee=third["mentee"],
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO session_participants "
                "(session_id, user_id, role, attendance_status) "
                "VALUES (:i, :u, 'mentor', 'attended')"
            ),
            {"i": hosted, "u": pair["mentee"]},
        )
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, current) == 0, "the hosting record leaked into the rate"


async def test_another_mentees_record_does_not_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The scoping check. A rate correlated on the wrong column — or on none —
    would return the platform's average, which reads plausibly and is wrong for
    everybody."""
    pair = await a_pair(db_engine, "rate-scope")
    stranger = await a_pair(db_engine, "rate-scope-other")
    await add_session(db_engine, pair, status="completed", mentee_attended="attended", days_ago=2)
    await add_session(
        db_engine,
        stranger,
        status="no_show",
        mentee_attended="no_show",
        days_ago=3,
        mentor=pair["mentor"],
    )
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    assert await rate(api_client, pair, current) == 100


async def test_the_rate_travels_with_every_row_of_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The list is where the request cards are rendered, so a rate present on
    the detail read and absent from the page would be present nowhere useful."""
    pair = await a_pair(db_engine, "rate-list")
    await add_session(db_engine, pair, status="completed", mentee_attended="attended", days_ago=2)
    await add_session(db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3)

    page = await api_client.get(
        f"/api/v1/users/{pair['mentor']}/sessions", headers=pair["mentor_headers"]
    )

    assert page.status_code == 200, page.text
    assert {row["mentee_attendance_rate"] for row in page.json()["data"]} == {100}


async def test_the_mentee_sees_their_own_rate(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Their own data, and the same number the mentor sees. One figure with two
    values by viewer would be worse than not showing it at all."""
    pair = await a_pair(db_engine, "rate-self")
    await add_session(db_engine, pair, status="completed", mentee_attended="attended", days_ago=2)
    current = await add_session(
        db_engine, pair, status="confirmed", mentee_attended="pending", days_ago=-3
    )

    mine = await api_client.get(f"/api/v1/sessions/{current}", headers=pair["mentee_headers"])

    assert mine.json()["mentee_attendance_rate"] == await rate(api_client, pair, current)

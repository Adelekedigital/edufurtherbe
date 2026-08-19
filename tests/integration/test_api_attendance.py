"""Arriving at a session, and what the session becomes once nobody else can.

Two halves that meet at one instant. `POST /sessions/{id}/join` is a party
saying *I am here*, allowed from five minutes before the start to fifteen after;
`settle_attendance` decides `completed` against `no_show` from the moment that
window shuts. The half-open boundary is what keeps them from overlapping, and
the unit suite holds that.

**Every session here has its `starts_at` moved after booking.** Slots are always
in the future, so a session at a joinable instant cannot be produced through the
API at all — and moving the row is honest where moving the machine's clock is
not.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_availability, add_session_type, make_public_mentor

from app.domain.attendance import JOIN_CLOSES, JOIN_OPENS, outcome
from app.domain.enums import SessionStatus
from app.infra.db.session_writer import settle_attendance
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_confirmed_session(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, *, starts_in: dt.timedelta
) -> dict[str, Any]:
    """A confirmed session starting `starts_in` from now.

    Booked through the endpoint that books, then moved: a fixture that inserted
    the row directly could describe a session the product cannot produce, which
    is how an attendance test ends up passing against a state that never occurs.
    """
    mentor = await make_public_mentor(engine, tag)
    session_type = await add_session_type(engine, mentor, duration=60, notice=0)
    for day in range(7):
        await add_availability(engine, mentor, day_of_week=day, start="00:00", end="23:00")
    mentor_auth, mentee_auth = uuid4(), uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": mentor_auth, "u": mentor}
        )
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Mo', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.test", "a": mentee_auth},
            )
        ).scalar_one()

    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    created = await client.post(
        "/api/v1/sessions",
        json={
            "session_type_id": str(session_type),
            "starts_at": str(slots.json()["data"][-1]["start"]),
        },
        headers=bearer(api_token(mentee_auth)) | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "confirmed"

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() + :offset WHERE id = :i"),
            {"offset": starts_in, "i": created.json()["id"]},
        )
    return {
        "id": created.json()["id"],
        "mentor": bearer(api_token(mentor_auth)),
        "mentee": bearer(api_token(mentee_auth)),
        "mentor_id": mentor,
        "mentee_id": mentee,
    }


def join_url(session: dict[str, Any]) -> str:
    return f"/api/v1/sessions/{session['id']}/join"


async def attendance(engine: AsyncEngine, session_id: str) -> dict[str, dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT role, attendance_status, joined_at "
                        "FROM session_participants WHERE session_id = :i"
                    ),
                    {"i": session_id},
                )
            )
            .mappings()
            .all()
        )
    return {row["role"]: dict(row) for row in rows}


async def status_of(engine: AsyncEngine, session_id: str) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("SELECT status FROM sessions WHERE id = :i"), {"i": session_id}
                )
            ).scalar_one()
        )


async def settle(engine: AsyncEngine) -> int:
    """Run the sweep the way the script does — its own session, then commit."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        settled = await settle_attendance(session, now=dt.datetime.now(dt.UTC))
        await session.commit()
    return settled


# --------------------------------------------------------------------------
# Arriving
# --------------------------------------------------------------------------


async def test_joining_inside_the_window_marks_you_present(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_confirmed_session(
        db_engine, api_client, "at-join", starts_in=dt.timedelta(minutes=1)
    )

    joined = await api_client.post(join_url(booking), headers=booking["mentee"])

    assert joined.status_code == 200, joined.text
    # `meeting_url` joined the response when the room adapter landed. Null here
    # because this fixture's mentor has no venue configured, which is a
    # different thing from being refused entry — the arrival is still recorded.
    assert joined.json() == {"joined": True, "meeting_url": None}
    rows = await attendance(db_engine, booking["id"])
    assert rows["mentee"]["attendance_status"] == "attended"
    assert rows["mentee"]["joined_at"] is not None


async def test_you_cannot_mark_the_other_party_present(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The scoping that matters here**, and it is not the usual one.

    Both parties may reach this endpoint on this session, so the `WHERE` cannot
    stop at "are you in it" — it names `user_id = actor_id`, because attendance
    drives both parties' reliability figures and marking somebody present is
    editing their record.
    """
    booking = await a_confirmed_session(
        db_engine, api_client, "at-only-mine", starts_in=dt.timedelta(minutes=1)
    )

    await api_client.post(join_url(booking), headers=booking["mentor"])

    rows = await attendance(db_engine, booking["id"])
    assert rows["mentor"]["attendance_status"] == "attended"
    assert rows["mentee"]["attendance_status"] == "pending"
    assert rows["mentee"]["joined_at"] is None


async def test_joining_twice_keeps_the_first_arrival(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A dropped call or a refreshed tab is the ordinary case. Without the
    `COALESCE` the column would record the last press, which is the one fact
    nobody wants."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-twice", starts_in=dt.timedelta(minutes=1)
    )

    await api_client.post(join_url(booking), headers=booking["mentee"])
    first = (await attendance(db_engine, booking["id"]))["mentee"]["joined_at"]
    again = await api_client.post(join_url(booking), headers=booking["mentee"])

    assert again.status_code == 200, again.text
    assert (await attendance(db_engine, booking["id"]))["mentee"]["joined_at"] == first


async def test_joining_before_the_window_opens_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A `409` rather than a quiet success: a client that believes it registered
    an arrival will not try again, and the party would be marked absent at a
    session they attended."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-early", starts_in=dt.timedelta(hours=2)
    )

    refused = await api_client.post(join_url(booking), headers=booking["mentee"])

    assert refused.status_code == 409, refused.text
    assert (await attendance(db_engine, booking["id"]))["mentee"]["attendance_status"] == "pending"


async def test_joining_after_the_window_shuts_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The other edge, and the one that protects the sweep: an arrival recorded
    after settlement would leave a `no_show` session with an `attended`
    participant — the exact contradiction `session_stats` already documents in
    the migrated data."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-late", starts_in=-dt.timedelta(hours=1)
    )

    refused = await api_client.post(join_url(booking), headers=booking["mentee"])

    assert refused.status_code == 409, refused.text


async def test_a_pending_request_cannot_be_joined(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Nobody agreed to it, so there is nothing to arrive at."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-pending", starts_in=dt.timedelta(minutes=1)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'pending_mentor_approval' WHERE id = :i"),
            {"i": booking["id"]},
        )

    refused = await api_client.post(join_url(booking), headers=booking["mentee"])

    assert refused.status_code == 409, refused.text


async def test_a_stranger_cannot_join(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_confirmed_session(
        db_engine, api_client, "at-stranger", starts_in=dt.timedelta(minutes=1)
    )
    stranger = uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, primary_role, timezone) "
                "VALUES (:e, :a, 'mentee', 'Africa/Lagos')"
            ),
            {"e": f"stranger-{stranger}@example.test", "a": stranger},
        )

    refused = await api_client.post(join_url(booking), headers=bearer(api_token(stranger)))

    assert refused.status_code == 404, refused.text


# --------------------------------------------------------------------------
# Settling — one test per outcome, which is what the plan asks for
# --------------------------------------------------------------------------


async def test_both_present_completes_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_confirmed_session(
        db_engine, api_client, "at-both", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(booking), headers=booking["mentor"])
    await api_client.post(join_url(booking), headers=booking["mentee"])
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": booking["id"]},
        )

    assert await settle(db_engine) == 1
    assert await status_of(db_engine, booking["id"]) == "completed"
    rows = await attendance(db_engine, booking["id"])
    assert {row["attendance_status"] for row in rows.values()} == {"attended"}


async def test_one_absent_misses_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The asymmetry between the two tables.** The session is `no_show`
    whichever party was missing, and *which* one it was stays on the participant
    rows — a session-level status cannot say both, and the per-person answer is
    the one the reliability figures read."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-one", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(booking), headers=booking["mentee"])
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": booking["id"]},
        )

    await settle(db_engine)

    assert await status_of(db_engine, booking["id"]) == "no_show"
    rows = await attendance(db_engine, booking["id"])
    assert rows["mentee"]["attendance_status"] == "attended"
    assert rows["mentor"]["attendance_status"] == "no_show"


async def test_both_absent_misses_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The third outcome, and the one no single reason code can express — which
    is why the event carries a coarse one and the participant rows carry the
    truth."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-neither", starts_in=-dt.timedelta(hours=1)
    )

    await settle(db_engine)

    assert await status_of(db_engine, booking["id"]) == "no_show"
    rows = await attendance(db_engine, booking["id"])
    assert {row["attendance_status"] for row in rows.values()} == {"no_show"}


async def test_the_settlement_agrees_with_the_rule(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The pin on a rule that exists twice.**

    `domain.attendance.outcome` is the specification and the settlement's `CASE`
    is the implementation, because a per-session loop would make a partial
    settlement reachable. Non-negotiable #8 permits two copies only when a test
    fails as they diverge; this drives all four combinations through both and
    compares, so a change to either without the other goes red.
    """
    combinations = [(True, True), (True, False), (False, True), (False, False)]
    bookings = []
    for index, (mentor_came, mentee_came) in enumerate(combinations):
        booking = await a_confirmed_session(
            db_engine, api_client, f"at-rule-{index}", starts_in=dt.timedelta(minutes=1)
        )
        if mentor_came:
            await api_client.post(join_url(booking), headers=booking["mentor"])
        if mentee_came:
            await api_client.post(join_url(booking), headers=booking["mentee"])
        bookings.append((booking, (mentor_came, mentee_came)))

    # **The cases the first version of this test could not reach**, and exactly
    # the ones the two copies disagreed on: a session with one participant row,
    # and one with none. Both are unreachable through booking — which writes
    # both rows in a single transaction — so the rows are removed deliberately.
    only_mentee = await a_confirmed_session(
        db_engine, api_client, "at-rule-partial", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(only_mentee), headers=only_mentee["mentee"])
    orphan = await a_confirmed_session(
        db_engine, api_client, "at-rule-orphan", starts_in=dt.timedelta(minutes=1)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM session_participants WHERE session_id = :i AND role = 'mentor'"),
            {"i": only_mentee["id"]},
        )
        await conn.execute(
            text("DELETE FROM session_participants WHERE session_id = :i"), {"i": orphan["id"]}
        )
    bookings += [(only_mentee, (False, True)), (orphan, (False, False))]

    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET starts_at = now() - interval '1 hour'"))
    assert await settle(db_engine) == len(bookings)

    for booking, (mentor_came, mentee_came) in bookings:
        assert (
            await status_of(db_engine, booking["id"])
            == outcome(mentor_attended=mentor_came, mentee_attended=mentee_came).value
        )


async def test_settling_is_safe_to_run_twice(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An external scheduler is the kind that fires twice, and a second event on
    a settled session would put a duplicate in a log that is meant to be the
    record of what happened."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-idem", starts_in=-dt.timedelta(hours=1)
    )
    assert await settle(db_engine) == 1

    assert await settle(db_engine) == 0

    events = await api_client.get(
        f"/api/v1/sessions/{booking['id']}/events", headers=booking["mentor"]
    )
    assert [event["to_status"] for event in events.json()["data"]] == ["confirmed", "no_show"]


async def test_a_session_still_in_its_window_is_left_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The boundary that matters most.** Settling a minute early marks a party
    absent while they still have fourteen minutes to arrive, and nothing later
    can undo it — joining after the window shuts is refused."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-open", starts_in=dt.timedelta(minutes=1)
    )

    assert await settle(db_engine) == 0
    assert await status_of(db_engine, booking["id"]) == "confirmed"
    assert (await attendance(db_engine, booking["id"]))["mentee"]["attendance_status"] == "pending"


async def test_a_cancelled_session_is_not_settled(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Nobody was expected, so nobody was absent. Without the `confirmed` filter
    every cancelled session in the database would be rewritten to `no_show` on
    the first run — which would also drag both parties' attendance rates down
    for sessions they had agreed not to hold."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-cancelled", starts_in=dt.timedelta(hours=2)
    )
    await api_client.post(f"/api/v1/sessions/{booking['id']}/cancel", headers=booking["mentee"])
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": booking["id"]},
        )

    assert await settle(db_engine) == 0
    assert await status_of(db_engine, booking["id"]) == "cancelled"
    assert (await attendance(db_engine, booking["id"]))["mentee"]["attendance_status"] == "pending"


async def test_the_settlement_event_has_no_human_actor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`actor_id` null with `actor_type` `system`, which the model's docstring
    calls honest for a sweep and better than inventing a system user. It is also
    what tells a mentee reading their timeline that nobody decided this."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-actor", starts_in=-dt.timedelta(hours=1)
    )
    await settle(db_engine)

    events = await api_client.get(
        f"/api/v1/sessions/{booking['id']}/events", headers=booking["mentee"]
    )

    (_, settled) = events.json()["data"]
    assert settled["actor_id"] is None
    assert settled["actor_type"] == "system"
    assert settled["from_status"] == SessionStatus.CONFIRMED.value
    # **Null when both were absent**: no code in the vocabulary can say it, and
    # inventing one would make an aggregate over `reason_code` wrong in a way
    # nobody could see. The participant rows are the only thing that can state
    # it, and they do.
    assert settled["reason_code"] is None


async def test_migrated_contradictions_are_left_exactly_as_they_are(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A session already `completed` with somebody absent is not corrected.**

    Three such rows exist in the loaded database, and `session_stats` documents
    them: the orphan trackers took a path that never consulted attendance, so
    "not cancelled" became "completed". They are the record of what the legacy
    app believed, and rewriting them would destroy the evidence that the two
    figures disagree.

    The `confirmed` filter is what leaves them alone, and it is asserted here
    rather than assumed because the obvious "fix the invariant everywhere" sweep
    would take them with it.
    """
    booking = await a_confirmed_session(
        db_engine, api_client, "at-legacy", starts_in=-dt.timedelta(hours=1)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'completed' WHERE id = :i"), {"i": booking["id"]}
        )
        await conn.execute(
            text(
                "UPDATE session_participants SET attendance_status = 'no_show' "
                "WHERE session_id = :i AND role = 'mentor'"
            ),
            {"i": booking["id"]},
        )

    assert await settle(db_engine) == 0
    assert await status_of(db_engine, booking["id"]) == "completed"
    assert (await attendance(db_engine, booking["id"]))["mentor"]["attendance_status"] == "no_show"


async def test_the_reason_code_names_the_party_who_was_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**These codes are what refund policy runs on**, so naming the wrong party
    is the wrong answer to the question that decides a refund — not merely
    coarse.

    The first version filed every absence under `mentee_no_show`, a mentor's
    included. Both one-sided cases are asserted, because a version that picked
    one code for both would still pass on whichever half it happened to choose.
    """
    mentor_missing = await a_confirmed_session(
        db_engine, api_client, "at-code-mentor", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(mentor_missing), headers=mentor_missing["mentee"])
    mentee_missing = await a_confirmed_session(
        db_engine, api_client, "at-code-mentee", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(mentee_missing), headers=mentee_missing["mentor"])
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET starts_at = now() - interval '1 hour'"))
    await settle(db_engine)

    async def code(booking: dict[str, Any]) -> Any:
        events = await api_client.get(
            f"/api/v1/sessions/{booking['id']}/events", headers=booking["mentor"]
        )
        return events.json()["data"][-1]["reason_code"]

    assert await code(mentor_missing) == "mentor_no_show"
    assert await code(mentee_missing) == "mentee_no_show"


async def test_a_completed_session_carries_no_reason_code(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Nothing to explain. A code here would put every delivered session into
    whatever aggregate the codes feed."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-code-none", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(booking), headers=booking["mentor"])
    await api_client.post(join_url(booking), headers=booking["mentee"])
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": booking["id"]},
        )
    await settle(db_engine)

    events = await api_client.get(
        f"/api/v1/sessions/{booking['id']}/events", headers=booking["mentor"]
    )
    assert events.json()["data"][-1]["reason_code"] is None


async def test_joining_a_session_with_no_record_for_you_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A silent success is the worst answer available here.**

    The update used to match zero rows and still report `{"joined": true}`, so
    the caller would be told their arrival was recorded, walk away, and be
    settled as absent with nothing to appeal against.
    """
    booking = await a_confirmed_session(
        db_engine, api_client, "at-norecord", starts_in=dt.timedelta(minutes=1)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM session_participants WHERE session_id = :i AND role = 'mentee'"),
            {"i": booking["id"]},
        )

    refused = await api_client.post(join_url(booking), headers=booking["mentee"])

    assert refused.status_code == 409, refused.text


async def test_a_session_nobody_was_recorded_at_did_not_happen(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The defect a probe found, asserted directly.**

    A confirmed session with no participant rows contains nobody who is absent,
    so the old "is anybody absent" reading settled it as `completed` — a session
    nobody was recorded at, reported as delivered and counted in the mentor's
    figures.
    """
    booking = await a_confirmed_session(
        db_engine, api_client, "at-orphan", starts_in=-dt.timedelta(hours=1)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM session_participants WHERE session_id = :i"), {"i": booking["id"]}
        )

    assert await settle(db_engine) == 1
    assert await status_of(db_engine, booking["id"]) == "no_show"


async def test_the_settlement_records_how_it_knew(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Written before anything reads it, and that is the justification.**

    A session settled today cannot later be re-examined for whether anybody
    observed it — so the fact is recorded at the moment the outcome is decided
    or it is lost. Every outcome is `reported` until a provider starts sending
    join and leave, and a payout rule can then require `observed` without a
    second status and without re-judging history.

    It is also `session_events.metadata`'s first writer; the JSONB column has
    been there since M4 with nothing putting anything in it.
    """
    booking = await a_confirmed_session(
        db_engine, api_client, "at-evidence", starts_in=-dt.timedelta(hours=1)
    )
    await settle(db_engine)

    async with db_engine.connect() as conn:
        recorded = (
            await conn.execute(
                text(
                    "SELECT metadata FROM session_events "
                    "WHERE session_id = :i ORDER BY created_at DESC LIMIT 1"
                ),
                {"i": booking["id"]},
            )
        ).scalar_one()

    assert recorded == {"evidence": "reported"}


async def test_only_the_settlement_claims_evidence(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A booking or a transition asserts nothing about attendance, so its event
    carries no evidence rather than an empty or default one — an absent key and
    a key saying "we do not know" are different claims, and only the first is
    true here."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-evidence-none", starts_in=dt.timedelta(hours=2)
    )

    async with db_engine.connect() as conn:
        creation = (
            await conn.execute(
                text(
                    "SELECT metadata FROM session_events "
                    "WHERE session_id = :i ORDER BY created_at LIMIT 1"
                ),
                {"i": booking["id"]},
            )
        ).scalar_one()

    assert creation == {}


async def test_the_session_says_when_its_window_opens_and_shuts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Sent rather than left to the client to compute.**

    The offsets are a product rule that will become a mentor preference, and a
    client hardcoding five and fifteen drifts from us the day that lands —
    silently, because nothing on either side would notice.
    """
    booking = await a_confirmed_session(
        db_engine, api_client, "at-window", starts_in=dt.timedelta(hours=2)
    )

    seen = (
        await api_client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    ).json()

    starts_at = dt.datetime.fromisoformat(seen["starts_at"])
    assert dt.datetime.fromisoformat(seen["join_opens_at"]) == starts_at - JOIN_OPENS
    assert dt.datetime.fromisoformat(seen["join_closes_at"]) == starts_at + JOIN_CLOSES


async def test_each_party_shows_whether_they_have_arrived(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The distinction the waiting screen needs.**

    "Your mentor has not joined yet" is a different message from "your mentor
    left", and a client cannot tell them apart from the session's own status —
    which stays `confirmed` throughout.
    """
    booking = await a_confirmed_session(
        db_engine, api_client, "at-arrived", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(booking), headers=booking["mentor"])

    seen = (
        await api_client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    ).json()

    assert seen["mentor"]["joined_at"] is not None
    assert seen["mentor"]["attendance_status"] == "attended"
    assert seen["mentee"]["joined_at"] is None
    assert seen["mentee"]["attendance_status"] == "pending"


async def test_a_party_with_no_attendance_record_reads_as_pending(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A missing participant row is not an arrival, and `pending` is the same
    answer an unsettled row gives — both mean *we do not know*, which is true of
    both. Two of the 105 migrated bookings are in exactly this state."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-norow", starts_in=dt.timedelta(hours=2)
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM session_participants WHERE session_id = :i"), {"i": booking["id"]}
        )

    seen = (
        await api_client.get(f"/api/v1/sessions/{booking['id']}", headers=booking["mentee"])
    ).json()

    assert seen["mentee"]["joined_at"] is None
    assert seen["mentee"]["attendance_status"] == "pending"


async def test_arrivals_travel_with_every_row_of_the_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The page is where the cards are, and a correlated subquery is what keeps
    it one statement — a join to `session_participants` would multiply rows
    before the limit and lose them at the cursor boundary."""
    booking = await a_confirmed_session(
        db_engine, api_client, "at-listed", starts_in=dt.timedelta(minutes=1)
    )
    await api_client.post(join_url(booking), headers=booking["mentee"])

    page = await api_client.get(
        f"/api/v1/users/{booking['mentee_id']}/sessions", headers=booking["mentee"]
    )

    assert page.status_code == 200, page.text
    (row,) = page.json()["data"]
    assert row["mentee"]["attendance_status"] == "attended"
    assert row["mentor"]["attendance_status"] == "pending"

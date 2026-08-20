"""Accept, decline, withdraw, cancel — and every way each of them is refused.

**Weighted towards the illegal transitions, deliberately.** The legal ones are
four lines each and would all pass against an endpoint that did no checking at
all; what this feature actually is, is the set of things it refuses. So there is
one test per refusal and one per success, rather than one per status code.

The rules live in `domain/sessions.py` and the four endpoints are four names for
that one table, so a test that a mentee cannot accept is also a test that the
table is being consulted.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type, make_public_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_booking(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, *, confirmed: bool = False
) -> dict[str, Any]:
    """A real booking, made through the endpoint that makes them.

    Seeded through the API rather than by raw insert so the fixture cannot
    describe a session the product could not produce — which is how a
    transition test ends up passing against a state that never occurs.
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
        if not confirmed:
            await conn.execute(
                text(
                    "UPDATE mentor_profiles SET requires_booking_confirmation = true "
                    "WHERE user_id = :u"
                ),
                {"u": mentor},
            )

    slots = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    # **The last slot in the window, not the first.** Cancelling is refused
    # within ten minutes of the start, and `notice=0` puts the first slot
    # minutes away — a fixture built on it would fail the cutoff for a reason
    # the test is not about.
    starts_at = str(slots.json()["data"][-1]["start"])
    created = await client.post(
        "/api/v1/sessions",
        json={"session_type_id": str(session_type), "starts_at": starts_at},
        headers=bearer(api_token(mentee_auth)) | {"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == ("confirmed" if confirmed else "pending_mentor_approval")
    return {
        "id": created.json()["id"],
        "mentor": bearer(api_token(mentor_auth)),
        "mentee": bearer(api_token(mentee_auth)),
        "mentor_id": mentor,
        "mentee_id": mentee,
        "starts_at": starts_at,
    }


def url(session: dict[str, Any], action: str) -> str:
    return f"/api/v1/sessions/{session['id']}/{action}"


async def status_of(engine: AsyncEngine, session_id: str) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("SELECT status FROM sessions WHERE id = :i"), {"i": session_id}
                )
            ).scalar_one()
        )


async def events_of(client: httpx.AsyncClient, session: dict[str, Any]) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/sessions/{session['id']}/events", headers=session["mentor"]
    )
    return list(response.json()["data"])


# --------------------------------------------------------------------------
# What each party may do
# --------------------------------------------------------------------------


async def test_the_mentor_accepts_and_the_session_is_confirmed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "tr-accept")

    accepted = await api_client.post(url(booking, "accept"), headers=booking["mentor"])

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"accepted": True}
    assert await status_of(db_engine, booking["id"]) == "confirmed"


async def test_the_mentor_declines_with_a_reason(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "tr-decline")

    declined = await api_client.post(
        url(booking, "decline"),
        json={"reason_code": "mentor_unavailable", "reason_text": "Conference clash"},
        headers=booking["mentor"],
    )

    assert declined.status_code == 200, declined.text
    assert await status_of(db_engine, booking["id"]) == "declined"
    (_, event) = await events_of(api_client, booking)
    assert event["reason_code"] == "mentor_unavailable"
    assert event["reason_text"] == "Conference clash"


async def test_the_mentee_withdraws_a_request_nobody_answered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "tr-withdraw")

    withdrawn = await api_client.post(url(booking, "withdraw"), headers=booking["mentee"])

    assert withdrawn.status_code == 200, withdrawn.text
    assert await status_of(db_engine, booking["id"]) == "withdrawn"


async def test_either_party_may_cancel_a_confirmed_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The only action with two actors**, and both sides are asserted because
    a query scoped on one of them would still pass a test that only tried the
    other."""
    by_mentor = await a_booking(db_engine, api_client, "tr-cancel-mentor", confirmed=True)
    by_mentee = await a_booking(db_engine, api_client, "tr-cancel-mentee", confirmed=True)

    theirs = await api_client.post(url(by_mentor, "cancel"), headers=by_mentor["mentor"])
    mine = await api_client.post(url(by_mentee, "cancel"), headers=by_mentee["mentee"])

    assert theirs.status_code == 200, theirs.text
    assert mine.status_code == 200, mine.text
    assert await status_of(db_engine, by_mentor["id"]) == "cancelled"
    assert await status_of(db_engine, by_mentee["id"]) == "cancelled"


# --------------------------------------------------------------------------
# What each party may not do
# --------------------------------------------------------------------------


async def test_a_mentee_may_never_accept_their_own_request(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The rule the whole `pending_mentor_approval` state exists for.**

    A `404`, not a `403`: `/accept` is the mentor's decision resource and to a
    mentee it does not exist, which is the same answer `require_admin` gives a
    non-admin. Nothing is hidden — the mentee can still read the session.
    """
    booking = await a_booking(db_engine, api_client, "tr-self-accept")

    refused = await api_client.post(url(booking, "accept"), headers=booking["mentee"])

    assert refused.status_code == 404, refused.text
    assert await status_of(db_engine, booking["id"]) == "pending_mentor_approval"


async def test_a_mentee_may_not_decline(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Declining and withdrawing reach the same status conceptually and are two
    statuses deliberately, so the mentee's route to ending a request is
    `withdraw` and this one is not theirs."""
    booking = await a_booking(db_engine, api_client, "tr-mentee-decline")

    refused = await api_client.post(url(booking, "decline"), headers=booking["mentee"])

    assert refused.status_code == 404, refused.text
    assert await status_of(db_engine, booking["id"]) == "pending_mentor_approval"


async def test_a_mentor_may_not_withdraw(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The mirror, and it matters for the same reason: a mentor ending a request
    is a **decline**, which is a fact about the mentor's reliability, and
    letting them record it as the mentee's withdrawal would move it off their
    own statistics."""
    booking = await a_booking(db_engine, api_client, "tr-mentor-withdraw")

    refused = await api_client.post(url(booking, "withdraw"), headers=booking["mentor"])

    assert refused.status_code == 404, refused.text


async def test_a_stranger_may_not_transition_a_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The object-level check, and it is in the query rather than after the
    fetch — a stranger's statement selects nothing at all."""
    booking = await a_booking(db_engine, api_client, "tr-stranger")
    stranger = uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, primary_role, timezone) "
                "VALUES (:e, :a, 'mentee', 'Africa/Lagos')"
            ),
            {"e": f"stranger-{stranger}@example.test", "a": stranger},
        )

    refused = await api_client.post(url(booking, "accept"), headers=bearer(api_token(stranger)))

    assert refused.status_code == 404, refused.text


async def test_transitioning_needs_a_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "tr-anon")

    assert (await api_client.post(url(booking, "accept"))).status_code == 401


# --------------------------------------------------------------------------
# What the state forbids
# --------------------------------------------------------------------------


async def test_accepting_twice_is_refused_rather_than_silently_succeeding(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A `409`, and the message names the state.**

    Reporting success would tell a client it had just confirmed something when
    nothing happened — which the institution-approval endpoint deliberately
    *does* do for a double click, and the difference is worth stating: there,
    re-approving reaches the same state and changes nothing. Here the second
    request could equally be arriving after a *cancellation*, and answering
    "accepted" to that is a lie.
    """
    booking = await a_booking(db_engine, api_client, "tr-twice")
    await api_client.post(url(booking, "accept"), headers=booking["mentor"])

    again = await api_client.post(url(booking, "accept"), headers=booking["mentor"])

    assert again.status_code == 409, again.text
    assert "confirmed" in again.json()["detail"]


async def test_a_confirmed_session_cannot_be_declined(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Declining answers a *request*. Once the mentor has agreed there is no
    request left to answer, and calling it off is `cancel` — which carries a
    different status because breaking an agreement is a different fact."""
    booking = await a_booking(db_engine, api_client, "tr-late-decline", confirmed=True)

    refused = await api_client.post(url(booking, "decline"), headers=booking["mentor"])

    assert refused.status_code == 409, refused.text
    assert await status_of(db_engine, booking["id"]) == "confirmed"


async def test_a_pending_request_cannot_be_cancelled(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The reverse, and the reason both directions are tested: `cancel` and
    `withdraw` are not interchangeable, and a client using the wrong one is told
    rather than quietly writing the wrong status into somebody's history."""
    booking = await a_booking(db_engine, api_client, "tr-early-cancel")

    refused = await api_client.post(url(booking, "cancel"), headers=booking["mentee"])

    assert refused.status_code == 409, refused.text
    assert await status_of(db_engine, booking["id"]) == "pending_mentor_approval"


async def test_a_cancelled_session_cannot_be_cancelled_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Terminal is terminal. Asserted because `cancelled` is not in any
    transition's `allowed_from`, and a table missing that guard would let a
    session be re-cancelled forever, each time writing another event."""
    booking = await a_booking(db_engine, api_client, "tr-recancel", confirmed=True)
    await api_client.post(url(booking, "cancel"), headers=booking["mentee"])

    again = await api_client.post(url(booking, "cancel"), headers=booking["mentee"])

    assert again.status_code == 409, again.text
    assert len(await events_of(api_client, booking)) == 2


async def test_a_session_cannot_be_cancelled_inside_the_cutoff(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The rule the plan proposed as a trigger.**

    It is here instead, because a `CHECK` cannot express it — the predicate must
    be `IMMUTABLE` and `now()` is `STABLE` — and a trigger would make every
    change to the number a migration. The project already settled that split:
    the database refuses what is impossible, the application refuses what is
    disallowed, which is why the 24-hour booking notice is a Pydantic bound over
    a wide sanity `CHECK`.

    The session is moved rather than the clock, because moving the clock means
    passing a different `now` and there is no route parameter for it.
    """
    booking = await a_booking(db_engine, api_client, "tr-cutoff", confirmed=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() + interval '5 minutes' WHERE id = :i"),
            {"i": booking["id"]},
        )

    refused = await api_client.post(url(booking, "cancel"), headers=booking["mentee"])

    assert refused.status_code == 409, refused.text
    assert "10 minutes" in refused.json()["detail"]
    assert await status_of(db_engine, booking["id"]) == "confirmed"


async def test_a_session_that_has_started_cannot_be_cancelled(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The comparison is one-sided on purpose. A session an hour into the past
    is past cancelling: what happened to it is `completed` or `no_show`, which
    the attendance sweep decides, not either party changing their mind."""
    booking = await a_booking(db_engine, api_client, "tr-started", confirmed=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '1 hour' WHERE id = :i"),
            {"i": booking["id"]},
        )

    refused = await api_client.post(url(booking, "cancel"), headers=booking["mentor"])

    assert refused.status_code == 409, refused.text


# --------------------------------------------------------------------------
# The reasons
# --------------------------------------------------------------------------


async def test_a_mentee_may_not_give_the_mentors_reason(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Authorization, not tidiness.** `SessionReasonCode` says the codes are
    what refund policy runs on — `mentor_unavailable` refunds where
    `mentee_no_longer_needed` does not — so a mentee free to send the mentor's
    code is a mentee who can claim a refund by choosing a value."""
    booking = await a_booking(db_engine, api_client, "tr-wrong-reason", confirmed=True)

    refused = await api_client.post(
        url(booking, "cancel"),
        json={"reason_code": "mentor_unavailable"},
        headers=booking["mentee"],
    )

    assert refused.status_code == 422, refused.text
    assert await status_of(db_engine, booking["id"]) == "confirmed"


async def test_a_mentor_may_not_claim_the_mentee_no_longer_needed_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The mirror. It is not the mentor's to assert about the mentee, and a
    mentor-side cancellation recorded under it would disappear from exactly the
    figure it should appear in."""
    booking = await a_booking(db_engine, api_client, "tr-wrong-reason-2", confirmed=True)

    refused = await api_client.post(
        url(booking, "cancel"),
        json={"reason_code": "mentee_no_longer_needed"},
        headers=booking["mentor"],
    )

    assert refused.status_code == 422, refused.text


async def test_no_party_may_give_a_system_reason(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`expired_no_response` belongs to the expiry producer, which is a system
    actor and not a person. A party asserting it about themselves would put a
    deliberate cancellation into the bucket used to measure abandonment."""
    booking = await a_booking(db_engine, api_client, "tr-system-reason", confirmed=True)

    refused = await api_client.post(
        url(booking, "cancel"),
        json={"reason_code": "expired_no_response"},
        headers=booking["mentor"],
    )

    assert refused.status_code == 422, refused.text


async def test_a_reason_is_optional(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    """Requiring one turns a clear-cut decision into a form to argue with, and
    every migrated event already carries none."""
    booking = await a_booking(db_engine, api_client, "tr-no-reason")

    declined = await api_client.post(url(booking, "decline"), headers=booking["mentor"])

    assert declined.status_code == 200, declined.text
    (_, event) = await events_of(api_client, booking)
    assert event["reason_code"] is None


# --------------------------------------------------------------------------
# The log, and the calendar
# --------------------------------------------------------------------------


async def test_every_transition_records_where_it_came_from(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The creation event has a null `from_status`; every later one names the
    state it left. Without that a timeline can say what a session became and
    never what it was, which is the question a dispute actually asks."""
    booking = await a_booking(db_engine, api_client, "tr-log")

    await api_client.post(url(booking, "accept"), headers=booking["mentor"])
    await api_client.post(url(booking, "cancel"), headers=booking["mentor"])

    created, accepted, cancelled = await events_of(api_client, booking)
    assert created["from_status"] is None
    assert (accepted["from_status"], accepted["to_status"]) == (
        "pending_mentor_approval",
        "confirmed",
    )
    assert (cancelled["from_status"], cancelled["to_status"]) == ("confirmed", "cancelled")
    assert cancelled["actor_id"] == str(booking["mentor_id"])


async def test_a_withdrawn_request_frees_its_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Newly true and worth asserting: nothing was agreed, so there is nothing
    to protect.

    It is also the one place a status change is visible in the public grid, and
    `_busy` filters no status at all — so this passes only because `withdrawn`
    sits outside `LIVE_STATUSES`, which is a fact about the exclusion constraint
    rather than about this endpoint.
    """
    booking = await a_booking(db_engine, api_client, "tr-free")
    async with db_engine.connect() as conn:
        mentor, session_type = (
            await conn.execute(
                text("SELECT mentor_id, session_type_id FROM sessions WHERE id = :i"),
                {"i": booking["id"]},
            )
        ).one()

    async def offered() -> list[str]:
        response = await api_client.get(
            f"/api/v1/users/{mentor}/availability/slots",
            params={"session_type_id": str(session_type)},
        )
        return [str(slot["start"]) for slot in response.json()["data"]]

    assert booking["starts_at"] not in await offered()
    await api_client.post(url(booking, "withdraw"), headers=booking["mentee"])
    assert booking["starts_at"] in await offered()


async def _mentor_and_offering(engine: AsyncEngine, session_id: str) -> tuple[str, str]:
    async with engine.connect() as conn:
        mentor, session_type = (
            await conn.execute(
                text("SELECT mentor_id, session_type_id FROM sessions WHERE id = :i"),
                {"i": session_id},
            )
        ).one()
    return str(mentor), str(session_type)


async def _offered_by(client: httpx.AsyncClient, mentor: str, session_type: str) -> list[str]:
    response = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": session_type},
    )
    return [str(slot["start"]) for slot in response.json()["data"]]


async def test_a_cancelled_session_frees_its_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Reversed, and this test used to assert the opposite.**

    It read: a mentor usually cancels *because* they are busy, so handing the
    hour back would book them into it again. Both halves are true and neither
    makes the hour occupied. What it produced was an hour hidden from the grid
    that `sessions_no_mentor_double_booking` — over `LIVE_STATUSES`, which
    excludes `cancelled` — would have accepted a booking for anyway, with
    nothing anywhere able to release it.

    A mentor who is genuinely not free says so, and the test below covers that.
    """
    booking = await a_booking(db_engine, api_client, "tr-frees", confirmed=True)
    mentor, session_type = await _mentor_and_offering(db_engine, booking["id"])

    await api_client.post(url(booking, "cancel"), headers=booking["mentor"])

    assert booking["starts_at"] in await _offered_by(api_client, mentor, session_type)


async def test_a_mentor_who_is_not_free_blocks_the_time_instead(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The answer is an availability exception, not hidden session state.

    Asserted through the grid **and** through the row, because the point is that
    the mentor now owns a thing they can see and delete — a private flag would
    make the grid identical and the calendar a mystery.
    """
    booking = await a_booking(db_engine, api_client, "tr-blocks", confirmed=True)
    mentor, session_type = await _mentor_and_offering(db_engine, booking["id"])

    response = await api_client.post(
        url(booking, "cancel"), headers=booking["mentor"], json={"release_slot": False}
    )

    assert response.status_code == 200, response.text
    assert booking["starts_at"] not in await _offered_by(api_client, mentor, session_type)
    async with db_engine.connect() as conn:
        blocks = (
            await conn.execute(
                text(
                    "SELECT type, reason FROM availability_exceptions "
                    "WHERE mentor_user_id = :m AND deleted_at IS NULL"
                ),
                {"m": mentor},
            )
        ).all()
    assert [(str(kind), reason) for kind, reason in blocks] == [
        ("block", "Kept from a cancelled session")
    ]


async def test_a_mentee_cannot_hold_a_mentors_hour(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The `FREES_THE_HOUR` defect, in the one place it could return.**

    A mentee cancelling says nothing about whether the mentor is free. If their
    `release_slot` were honoured, booking-and-cancelling would empty a mentor's
    calendar an hour at a time — the same mentee-driven denial of service that
    `withdrawn` once was, wearing a different status.

    Ignored rather than refused: the field is not theirs to answer, and a `422`
    would teach a client to send a value it should never have had an opinion
    about.
    """
    booking = await a_booking(db_engine, api_client, "tr-mentee-hold", confirmed=True)
    mentor, session_type = await _mentor_and_offering(db_engine, booking["id"])

    response = await api_client.post(
        url(booking, "cancel"), headers=booking["mentee"], json={"release_slot": False}
    )

    assert response.status_code == 200, response.text
    assert booking["starts_at"] in await _offered_by(api_client, mentor, session_type)
    async with db_engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM availability_exceptions WHERE mentor_user_id = :m"),
                {"m": mentor},
            )
        ).scalar_one() == 0


async def test_cancelling_with_no_body_frees_the_hour(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The default, exercised through the path a client most easily takes.

    `release_slot` defaults to releasing because the two ways of being wrong are
    not equally visible: an hour offered while the mentor is busy arrives as a
    booking they can decline, where an hour withheld while they are free arrives
    as nothing at all.
    """
    booking = await a_booking(db_engine, api_client, "tr-nobody", confirmed=True)
    mentor, session_type = await _mentor_and_offering(db_engine, booking["id"])

    await api_client.post(url(booking, "cancel"), headers=booking["mentor"])

    assert booking["starts_at"] in await _offered_by(api_client, mentor, session_type)


async def test_a_session_crossing_the_mentors_midnight_can_still_be_cancelled(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The edge case that would otherwise be a 500 on a legitimate action.**

    A mentor in Lagos whose availability is declared in New York time can hold a
    session that spans midnight where they live. `exception_window_ordered`
    requires `end_time > start_time`, so one row could not express it and the
    cancellation would fail — on a session the mentor is entitled to call off,
    for a reason that is not their fault.

    Two rows, and the block still covers exactly the session's hours.
    """
    booking = await a_booking(db_engine, api_client, "tr-midnight", confirmed=True)
    mentor, _ = await _mentor_and_offering(db_engine, booking["id"])
    # 22:30Z for 90 minutes is 23:30-01:00 in Lagos, and 18:30-20:00 in New York
    # — one ordered window there, two local days for the mentor.
    crossing = dt.datetime.fromisoformat(booking["starts_at"]).replace(
        hour=22, minute=30, second=0, microsecond=0
    ) + dt.timedelta(days=1)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = :s, duration_minutes = 90 WHERE id = :i"),
            {"s": crossing, "i": booking["id"]},
        )
        await conn.execute(
            text("UPDATE users SET timezone = 'Africa/Lagos' WHERE id = :u"), {"u": mentor}
        )

    response = await api_client.post(
        url(booking, "cancel"), headers=booking["mentor"], json={"release_slot": False}
    )

    assert response.status_code == 200, response.text
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT lower(date_range) AS day, start_time, end_time "
                    "FROM availability_exceptions WHERE mentor_user_id = :m "
                    "ORDER BY lower(date_range)"
                ),
                {"m": mentor},
            )
        ).all()
    assert len(rows) == 2, rows
    assert rows[0].start_time == dt.time(23, 30)
    assert rows[0].end_time == dt.time.max
    assert rows[1].start_time == dt.time(0, 0)
    assert rows[1].end_time == dt.time(1, 0)
    assert rows[1].day == rows[0].day + dt.timedelta(days=1)


async def test_removing_the_block_returns_the_hour(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The whole reason it is an exception rather than session state.

    A mentor who changes their mind deletes an ordinary block on their own
    availability. Nothing about cancellation has to be undone, and no endpoint
    exists for undoing it.
    """
    booking = await a_booking(db_engine, api_client, "tr-unblock", confirmed=True)
    mentor, session_type = await _mentor_and_offering(db_engine, booking["id"])
    await api_client.post(
        url(booking, "cancel"), headers=booking["mentor"], json={"release_slot": False}
    )

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE availability_exceptions SET deleted_at = now() WHERE mentor_user_id = :m"),
            {"m": mentor},
        )

    assert booking["starts_at"] in await _offered_by(api_client, mentor, session_type)


# --------------------------------------------------------------------------
# The participant rows booking now writes
# --------------------------------------------------------------------------


async def test_booking_writes_both_participant_rows(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A gap the booking pull request left**, fixed here rather than
    separately because the attendance sweep is the next thing and it needs these
    rows to exist.

    `SessionParticipant`'s own docstring says they are written in the same
    transaction as the session, so that they can never disagree with
    `mentor_id` and `mentee_id` — and until now nothing wrote them at all, which
    made that a claim rather than a description.

    Both start `pending`: "we do not know yet" is a real answer and
    distinguishable from `no_show`.
    """
    booking = await a_booking(db_engine, api_client, "tr-participants")

    async with db_engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT user_id, role, attendance_status, joined_at "
                        "FROM session_participants WHERE session_id = :i ORDER BY role"
                    ),
                    {"i": booking["id"]},
                )
            )
            .mappings()
            .all()
        )

    assert [row["role"] for row in rows] == ["mentee", "mentor"]
    assert {row["user_id"] for row in rows} == {booking["mentee_id"], booking["mentor_id"]}
    assert {row["attendance_status"] for row in rows} == {"pending"}
    assert {row["joined_at"] for row in rows} == {None}


async def test_a_retry_does_not_write_a_second_pair_of_participants(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The replay returns the stored answer and runs no insert, so the unique
    `(session_id, user_id)` is never even reached. Asserted anyway: a future
    change that replayed by *re-running* the booking would violate it, and a
    `500` on a retry is the worst place to discover a design change."""
    booking = await a_booking(db_engine, api_client, "tr-participants-retry")
    async with db_engine.connect() as conn:
        before = (
            await conn.execute(
                text("SELECT count(*) FROM session_participants WHERE session_id = :i"),
                {"i": booking["id"]},
            )
        ).scalar_one()

    assert before == 2

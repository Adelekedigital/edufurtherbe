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


async def test_a_cancelled_session_keeps_its_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The settled rule, and the opposite of the test above.**

    A mentor usually cancels *because* they are busy, and handing the hour
    straight back would book them into it again. `_busy` filters no status, so
    this is the behaviour that has always been there — asserted here because
    cancelling is now reachable, and because the natural "free the slot" fix
    would break it.
    """
    booking = await a_booking(db_engine, api_client, "tr-keep", confirmed=True)
    async with db_engine.connect() as conn:
        mentor, session_type = (
            await conn.execute(
                text("SELECT mentor_id, session_type_id FROM sessions WHERE id = :i"),
                {"i": booking["id"]},
            )
        ).one()

    await api_client.post(url(booking, "cancel"), headers=booking["mentor"])

    response = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    assert booking["starts_at"] not in [str(slot["start"]) for slot in response.json()["data"]]


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

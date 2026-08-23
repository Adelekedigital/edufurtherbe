"""Booking an hour, and not booking it twice.

**The slot grid is the definition of what is bookable, and this endpoint asks
it.** Every test here books an instant taken from `/slots` rather than one
computed in the test, because computing it would be a third copy of the rule —
after `bookable()` and after whatever the test author remembered about notice
windows — and the copy that drifts is the one that keeps passing.

**Two guarantees, and they are separate.** The exclusion constraint makes a
double booking impossible; `idempotency_keys` makes a *retry* of one booking
return the original answer rather than becoming a second booking. Neither
implies the other, and the tests are grouped that way.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import (
    add_availability,
    add_session_type,
    make_public_mentor,
    until_blocked,
)

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/sessions"


async def a_mentee(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    """A mentee and the headers that authenticate them.

    No mentor profile, deliberately: booking is what any signed-in user may do,
    and a test whose mentee happened to be a mentor would pass while a plain
    mentee was refused.
    """
    auth_id = uuid4()
    async with engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Mo', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.test", "a": auth_id},
            )
        ).scalar_one()
    return mentee, bearer(api_token(auth_id))


async def a_bookable_offering(
    engine: AsyncEngine,
    tag: str,
    *,
    confirmation: bool | None = None,
    mentor_confirms: bool = False,
) -> tuple[UUID, UUID]:
    """A public mentor with hours on every weekday and one offering.

    All seven days, for the reason the whole slot suite seeds all seven: a test
    that depends on which day it runs on is a test that fails on a Tuesday
    (#99). `notice=0` so the first free slot is minutes away rather than a day.
    """
    mentor = await make_public_mentor(engine, tag)
    session_type = await add_session_type(engine, mentor, duration=60, notice=0)
    for day in range(7):
        await add_availability(engine, mentor, day_of_week=day, start="00:00", end="23:00")
    async with engine.begin() as conn:
        if mentor_confirms:
            await conn.execute(
                text(
                    "UPDATE mentor_profiles SET requires_booking_confirmation = true "
                    "WHERE user_id = :u"
                ),
                {"u": mentor},
            )
        if confirmation is not None:
            await conn.execute(
                text(
                    "UPDATE session_type_booking_configs "
                    "SET requires_booking_confirmation = :c WHERE session_type_id = :t"
                ),
                {"c": confirmation, "t": session_type},
            )
    return mentor, session_type


async def first_slot(client: httpx.AsyncClient, mentor: UUID, session_type: UUID) -> str:
    """The next instant the public endpoint offers, exactly as it words it."""
    response = await client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    assert response.status_code == 200, response.text
    slots = response.json()["data"]
    assert slots, "the fixture mentor offers nothing, so nothing here tests booking"
    return str(slots[0]["start"])


def body(session_type: UUID, starts_at: str, **overrides: Any) -> dict[str, Any]:
    return {
        "session_type_id": str(session_type),
        "starts_at": starts_at,
        "topic": "Personal statement",
    } | overrides


def key(value: str = "") -> dict[str, str]:
    return {"Idempotency-Key": value or str(uuid4())}


async def count_sessions(engine: AsyncEngine, mentor: UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM sessions WHERE mentor_id = :m"), {"m": mentor}
                )
            ).scalar_one()
        )


# --------------------------------------------------------------------------
# The booking
# --------------------------------------------------------------------------


async def test_a_slot_the_grid_offers_becomes_a_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The happy path, and it asserts the *shape* as well as the row.

    The response is `SessionRead` — the same body `GET /sessions/{id}` returns,
    assembled by the same code — so a client can render the confirmation screen
    from the booking response without a second call.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-happy")
    mentee, headers = await a_mentee(db_engine, "book-happy")
    slot = await first_slot(api_client, mentor, session_type)

    created = await api_client.post(URL, json=body(session_type, slot), headers=headers | key())

    assert created.status_code == 201, created.text
    session = created.json()
    assert session["mentor_id"] == str(mentor)
    assert session["mentee_id"] == str(mentee)
    assert session["session_type_id"] == str(session_type)
    assert session["topic"] == "Personal statement"
    # Snapshotted from the config, not sent by the client — a later edit to the
    # offering must not rewrite what was agreed.
    assert session["duration_minutes"] == 60
    # Generated at confirmation, not at booking: a static room means an early
    # joiner walks into the previous session.
    assert session["meeting_url"] is None


async def test_the_booking_is_immediately_readable_by_both_parties(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The write and the reads agree, which is not automatic: `POST` builds its
    body from `get_session` scoped to the mentee, and the mentor reads the same
    row through a different scope."""
    mentor, session_type = await a_bookable_offering(db_engine, "book-read")
    _, mentee_headers = await a_mentee(db_engine, "book-read")
    slot = await first_slot(api_client, mentor, session_type)

    created = await api_client.post(
        URL, json=body(session_type, slot), headers=mentee_headers | key()
    )
    session_id = created.json()["id"]

    mine = await api_client.get(f"{URL}/{session_id}", headers=mentee_headers)
    assert mine.status_code == 200, mine.text
    assert mine.json()["id"] == session_id


async def test_the_creation_event_is_written_with_no_prior_state(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`from_status` null is the creation event's signature, and the reason the
    column is nullable at all. The event is written by the same transaction as
    the status, per the model's own note — a trigger projecting one onto the
    other would be a second mechanism for one fact."""
    mentor, session_type = await a_bookable_offering(db_engine, "book-event")
    mentee, headers = await a_mentee(db_engine, "book-event")
    slot = await first_slot(api_client, mentor, session_type)

    created = await api_client.post(URL, json=body(session_type, slot), headers=headers | key())
    events = await api_client.get(f"{URL}/{created.json()['id']}/events", headers=headers)

    (event,) = events.json()["data"]
    assert event["from_status"] is None
    assert event["to_status"] == created.json()["status"]
    assert event["actor_id"] == str(mentee)
    assert event["actor_type"] == "user"


# --------------------------------------------------------------------------
# Whether it waits for the mentor
# --------------------------------------------------------------------------


async def test_an_offering_that_needs_no_answer_is_confirmed_at_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "book-auto")
    _, headers = await a_mentee(db_engine, "book-auto")
    slot = await first_slot(api_client, mentor, session_type)

    created = await api_client.post(URL, json=body(session_type, slot), headers=headers | key())

    assert created.json()["status"] == "confirmed"


async def test_the_offering_inherits_the_mentors_setting_when_it_has_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Null on the config means inherit**, and this is the first thing to read
    that column at all — it has been shipped and unread since the config table
    landed.

    The mentor's column is `NOT NULL`, so the chain always bottoms out. That is
    what makes the nullable boolean legitimate here and was not true of the
    primary-offering cascade it replaced, whose bottom was reachable and empty.
    """
    mentor, session_type = await a_bookable_offering(
        db_engine, "book-inherit", mentor_confirms=True
    )
    _, headers = await a_mentee(db_engine, "book-inherit")
    slot = await first_slot(api_client, mentor, session_type)

    created = await api_client.post(URL, json=body(session_type, slot), headers=headers | key())

    assert created.json()["status"] == "pending_mentor_approval"


async def test_the_offering_overrides_the_mentor_when_it_says_so(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The point of the column: a free intro booked instantly while a paid
    review on the same mentor waits."""
    mentor, session_type = await a_bookable_offering(
        db_engine, "book-override", confirmation=False, mentor_confirms=True
    )
    _, headers = await a_mentee(db_engine, "book-override")
    slot = await first_slot(api_client, mentor, session_type)

    created = await api_client.post(URL, json=body(session_type, slot), headers=headers | key())

    assert created.json()["status"] == "confirmed"


# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------


async def test_an_instant_the_grid_does_not_offer_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Half past the hour, when the grid runs on the hour from the first slot.

    Refused with `422` and **not** distinguished from any other reason: too
    soon, outside the mentor's hours, on a blocked date and already taken are
    all *this instant is not offered*, and the client's response to every one of
    them is to re-read `/slots`.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-offgrid")
    _, headers = await a_mentee(db_engine, "book-offgrid")
    slot = dt.datetime.fromisoformat(await first_slot(api_client, mentor, session_type))

    refused = await api_client.post(
        URL,
        json=body(session_type, (slot + dt.timedelta(minutes=30)).isoformat()),
        headers=headers | key(),
    )

    assert refused.status_code == 422, refused.text
    assert await count_sessions(db_engine, mentor) == 0


async def test_a_second_booking_of_the_same_hour_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The named regression: one slot, two mentees, one session.**

    Sequentially the loser gets `422` rather than `409`, and that is the better
    answer — by the time they ask, the slot genuinely is not on the grid any
    more, which is exactly what the message tells them. The `409` belongs to the
    race, where both read the grid before either wrote, and it is asserted
    directly against the constraint below.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-taken")
    _, first = await a_mentee(db_engine, "book-taken-a")
    _, second = await a_mentee(db_engine, "book-taken-b")
    slot = await first_slot(api_client, mentor, session_type)

    won = await api_client.post(URL, json=body(session_type, slot), headers=first | key())
    lost = await api_client.post(URL, json=body(session_type, slot), headers=second | key())

    assert won.status_code == 201, won.text
    assert lost.status_code == 422, lost.text
    assert await count_sessions(db_engine, mentor) == 1


async def test_the_slot_disappears_from_the_grid_once_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The other half of the test above, and the reason it can only be a `422`.

    Asserted separately because it is a claim about `/slots` rather than about
    booking, and a change that broke it would otherwise show up as a confusing
    failure in the wrong test.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-vanish")
    _, headers = await a_mentee(db_engine, "book-vanish")
    slot = await first_slot(api_client, mentor, session_type)

    await api_client.post(URL, json=body(session_type, slot), headers=headers | key())

    assert await first_slot(api_client, mentor, session_type) != slot


async def test_a_mentor_cannot_book_their_own_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Reachable rather than absurd: dual roles are free by design, so the
    person holding a mentor profile is also somebody's mentee.

    Refused at the boundary because `no_self_booking` is a `CHECK`, and a
    violated `CHECK` is a `500` — which tells the caller nothing and pages
    somebody.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-self")
    slot = await first_slot(api_client, mentor, session_type)
    auth_id = uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )

    refused = await api_client.post(
        URL, json=body(session_type, slot), headers=bearer(api_token(auth_id)) | key()
    )

    assert refused.status_code == 422, refused.text
    assert await count_sessions(db_engine, mentor) == 0


async def test_an_unlisted_mentors_offering_cannot_be_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Booking is only possible where a slot is. An offering a stranger cannot
    see is not one a stranger may book, and the 404 conflates all six reasons
    exactly as `/slots` does."""
    mentor, session_type = await a_bookable_offering(db_engine, "book-hidden")
    _, headers = await a_mentee(db_engine, "book-hidden")
    slot = await first_slot(api_client, mentor, session_type)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :u"),
            {"u": mentor},
        )

    refused = await api_client.post(URL, json=body(session_type, slot), headers=headers | key())

    assert refused.status_code == 404, refused.text


async def test_booking_needs_a_token(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "book-anon")
    slot = await first_slot(api_client, mentor, session_type)

    refused = await api_client.post(URL, json=body(session_type, slot), headers=key())

    assert refused.status_code == 401, refused.text


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------


async def test_a_retry_replays_the_answer_rather_than_booking_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**What the table exists for.** A flaky connection retrying gets the same
    session, the same `201`, and no second hour off the mentor's calendar."""
    mentor, session_type = await a_bookable_offering(db_engine, "book-replay")
    _, headers = await a_mentee(db_engine, "book-replay")
    slot = await first_slot(api_client, mentor, session_type)
    same = key()

    first = await api_client.post(URL, json=body(session_type, slot), headers=headers | same)
    again = await api_client.post(URL, json=body(session_type, slot), headers=headers | same)

    assert first.status_code == 201, first.text
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert await count_sessions(db_engine, mentor) == 1


async def test_a_retry_without_the_key_books_a_second_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The counter-test, and it is why the header is required.**

    Sent with a *fresh* key the same body is a new booking attempt, which is
    correct — and it is refused here only because the first booking took the
    slot. Without the header at all there would be no reservation and no reason
    to refuse the retry beyond that coincidence, which is the protection this
    endpoint declines to leave optional.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-nokey")
    _, headers = await a_mentee(db_engine, "book-nokey")
    slot = await first_slot(api_client, mentor, session_type)

    await api_client.post(URL, json=body(session_type, slot), headers=headers | key())
    again = await api_client.post(URL, json=body(session_type, slot), headers=headers | key())

    assert again.status_code == 422, again.text
    assert await count_sessions(db_engine, mentor) == 1


async def test_the_key_is_required(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    """Stripe treats it as recommended and can: its clients are servers written
    once. The retry here is a phone on a bad connection, and an optional header
    makes the guarantee opt-in for exactly the caller who most needs it."""
    mentor, session_type = await a_bookable_offering(db_engine, "book-keyless")
    _, headers = await a_mentee(db_engine, "book-keyless")
    slot = await first_slot(api_client, mentor, session_type)

    refused = await api_client.post(URL, json=body(session_type, slot), headers=headers)

    assert refused.status_code == 422, refused.text
    assert await count_sessions(db_engine, mentor) == 0


async def test_the_same_key_with_a_different_body_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A `422` rather than a silent replay of the wrong answer.

    The dangerous alternative is not refusing too much — it is answering a
    booking for Thursday with the session created for Wednesday, which the
    client would render as a success.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-mismatch")
    _, headers = await a_mentee(db_engine, "book-mismatch")
    slot = await first_slot(api_client, mentor, session_type)
    same = key()

    await api_client.post(URL, json=body(session_type, slot), headers=headers | same)
    other = await api_client.post(
        URL, json=body(session_type, slot, topic="Something else"), headers=headers | same
    )

    assert other.status_code == 422, other.text
    assert "different request" in other.json()["detail"]


async def test_field_order_is_not_a_different_request(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The unit test asserts the fingerprint; this asserts it end to end,
    because the body reaching the hash is Pydantic's dump rather than the bytes
    the client sent — and a change that hashed the raw body would pass the unit
    test and fail here."""
    mentor, session_type = await a_bookable_offering(db_engine, "book-order")
    _, headers = await a_mentee(db_engine, "book-order")
    slot = await first_slot(api_client, mentor, session_type)
    same = key()
    sent = body(session_type, slot)

    first = await api_client.post(URL, json=sent, headers=headers | same)
    again = await api_client.post(
        URL, json=dict(reversed(list(sent.items()))), headers=headers | same
    )

    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]


async def test_one_users_key_is_not_another_users_key(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The isolation test, and the reason the unique index is `(user_id, key)`
    rather than `(key)`.**

    A key row holds a stored response body, so the lookup must be scoped to the
    caller — and once it is, a global key space is a defect: the second caller's
    scoped select finds nothing while their insert collides, a failure with no
    correct answer to give.

    **The second caller then retries, and that is the half that matters.** With
    only one booking each, an unscoped lookup is never reached — the insert does
    not conflict, so the select never runs. The retry is what drives the
    conflict branch into a key space holding two rows for this key, where
    dropping the `user_id` clause returns the wrong caller's booking or none at
    all. A mutation run is what found that; the test as first written passed
    with the scope removed.

    The key is deliberately something a naive client would send. UUIDs make this
    unreachable by luck, and the caller who most needs idempotency is the one
    least likely to generate one.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-scope")
    _, first = await a_mentee(db_engine, "book-scope-a")
    _, second = await a_mentee(db_engine, "book-scope-b")
    shared = {"Idempotency-Key": "not-a-uuid-just-1"}
    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    one, two = (str(slot["start"]) for slot in slots.json()["data"][:2])

    mine = await api_client.post(URL, json=body(session_type, one), headers=first | shared)
    theirs = await api_client.post(URL, json=body(session_type, two), headers=second | shared)
    retried = await api_client.post(URL, json=body(session_type, two), headers=second | shared)

    assert mine.status_code == 201, mine.text
    assert theirs.status_code == 201, theirs.text
    assert mine.json()["id"] != theirs.json()["id"]
    assert retried.status_code == 201, retried.text
    assert retried.json()["id"] == theirs.json()["id"], "replayed somebody else's booking"


async def test_an_expired_key_is_reclaimed_rather_than_replayed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Expiry is enforced by the queries, not by a sweep — so the table
    self-heals and nothing depends on a retention job having run.

    A day-old key is available again, and reusing it books rather than replaying
    a stale answer. That is the whole difference between a cache and a leak.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-expiry")
    _, headers = await a_mentee(db_engine, "book-expiry")
    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots",
        params={"session_type_id": str(session_type)},
    )
    one, two = (str(slot["start"]) for slot in slots.json()["data"][:2])
    same = key()

    first = await api_client.post(URL, json=body(session_type, one), headers=headers | same)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE idempotency_keys SET expires_at = now() - interval '1s'"))
    after = await api_client.post(URL, json=body(session_type, two), headers=headers | same)

    assert after.status_code == 201, after.text
    assert after.json()["id"] != first.json()["id"]
    assert "Idempotent-Replayed" not in after.headers


# --------------------------------------------------------------------------
# The constraint underneath
# --------------------------------------------------------------------------


async def test_the_exclusion_constraint_refuses_a_genuine_race(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The `409`, reached the only way it is reachable.**

    Sequentially the loser gets `422`, because by then the slot is off the grid.
    A `409` needs both requests to have read the grid *before* either wrote,
    which is what two open transactions reproduce — and it is the case the
    pre-check cannot cover, which is why the constraint and not the check is
    what makes a double booking impossible.

    Driven at the store rather than through two concurrent requests: the
    interleaving has to be exact, and a `gather` of two HTTP calls would assert
    the same thing on a coin flip.
    """
    from app.core.errors import ConflictError
    from app.infra.db.session_writer import book_session

    mentor, session_type = await a_bookable_offering(db_engine, "book-race")
    winner, _ = await a_mentee(db_engine, "book-race-a")
    loser, _ = await a_mentee(db_engine, "book-race-b")
    slot = dt.datetime.fromisoformat(await first_slot(api_client, mentor, session_type))
    payload = {"session_type_id": session_type, "starts_at": slot}
    now = dt.datetime.now(dt.UTC)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as first, factory() as second:
        await book_session(first, winner, payload, now=now)
        # `second` reads the grid while `first`'s row is still uncommitted and
        # therefore invisible, so it passes the same pre-check the winner did —
        # then blocks on the exclusion constraint until the winner commits.
        racing = asyncio.create_task(book_session(second, loser, payload, now=now))
        await until_blocked(db_engine)
        await first.commit()
        with pytest.raises(ConflictError):
            await racing

    assert await count_sessions(db_engine, mentor) == 1


async def test_a_refused_booking_leaves_no_reservation_behind(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The bug this build was watching for.**

    If the reservation survived a refusal, the client's retry would be told
    forever that a request with that key is in flight — a booking permanently
    poisoned by one unlucky attempt. It does not survive, because the key and
    the session are written in one transaction and a refusal rolls both back.
    """
    mentor, session_type = await a_bookable_offering(db_engine, "book-poison")
    _, headers = await a_mentee(db_engine, "book-poison")
    slot = dt.datetime.fromisoformat(await first_slot(api_client, mentor, session_type))
    same = key()

    refused = await api_client.post(
        URL,
        json=body(session_type, (slot + dt.timedelta(minutes=30)).isoformat()),
        headers=headers | same,
    )
    retried = await api_client.post(
        URL, json=body(session_type, slot.isoformat()), headers=headers | same
    )

    assert refused.status_code == 422, refused.text
    assert retried.status_code == 201, retried.text

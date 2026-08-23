"""Writing a review, correcting it, and being told why not.

Three rules meet on this surface and each can refuse a request that is perfectly
well formed:

    one review per session          terminal — the answer never changes
    one per offering per interval   temporary — it changes when the window passes
    ten minutes to correct          temporary in the other direction

**The two `409`s carry problem types, and that is what these tests pin.** A
client that cannot tell "never" from "not yet" either retries forever or
abandons a review it could have written next month, and the message text is not
something a client may branch on. Settled decision #110 shipped a `409` without
a type and named this exact condition as the trigger for adding one.

Sessions are inserted `completed` rather than driven through the lifecycle,
following `factories.add_completed_sessions`: the state is producible and the
route under test reads `status`, so booking and settling each one would add a
minute of runtime and no coverage.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_session_type, make_public_mentor, until_blocked

from app.core.errors import AlreadyReviewedError, NotFoundError
from app.domain.reviews import REVIEW_EDIT_WINDOW, REVIEW_INTERVAL
from app.infra.db.review_writer import edit_review, write_review
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ALREADY = "/problems/review-already-exists"
TOO_SOON = "/problems/review-interval-not-elapsed"

#: A well-formed body, as words rather than numbers. Every test that is not
#: about validation starts from this and changes one thing.
BODY: dict[str, Any] = {
    "communication_rating": "excellent",
    "knowledge_rating": "great",
    "practicality_rating": "excellent",
    "support_rating": "great",
    "valuable_rating": 5,
    "nps_recommend_score": 9,
    "public_review": "He showed me what a strong portfolio actually looks like.",
}


@dataclass
class World:
    """One mentor with two offerings, and one mentee holding a token."""

    engine: AsyncEngine
    client: httpx.AsyncClient
    mentor: UUID
    mentee: UUID
    headers: dict[str, str]
    offering_a: UUID
    offering_b: UUID

    async def completed(self, offering: UUID | None, *, days_ago: int = 1) -> str:
        """A completed session of this mentee's, on this offering."""
        async with self.engine.begin() as conn:
            row = await conn.execute(
                text(
                    "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, "
                    "starts_at, duration_minutes, status) "
                    "VALUES (:m, :e, :t, :starts, 45, 'completed') RETURNING id"
                ),
                {
                    "m": self.mentor,
                    "e": self.mentee,
                    "t": offering,
                    "starts": dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago, hours=days_ago),
                },
            )
        return str(row.scalar_one())

    async def review(self, session_id: str, **changes: Any) -> httpx.Response:
        return await self.client.post(
            "/api/v1/reviews",
            json=BODY | {"session_id": session_id} | changes,
            headers=self.headers,
        )

    async def offered(self, mentor: UUID | None = None) -> list[dict[str, Any]]:
        """The `data` of the reviewable-sessions page.

        Behind a helper so the envelope itself is asserted in exactly one place —
        `test_the_list_comes_back_in_the_envelope` — rather than incidentally by
        every test that reads the list.
        """
        query = f"?mentor_id={mentor}" if mentor is not None else ""
        response = await self.client.get(
            f"/api/v1/me/reviewable-sessions{query}", headers=self.headers
        )
        return list(response.json()["data"])

    async def age(self, review_id: str, by: dt.timedelta) -> None:
        """Move a review's `created_at` back, which is how a window is crossed.

        The trigger overwrites `updated_at` on every write and cannot be told
        not to, so `created_at` is moved on its own — which is also the sharper
        test: the two windows must read `created_at`, and a fixture that moved
        both would pass even if they read `updated_at`.
        """
        async with self.engine.begin() as conn:
            await conn.execute(
                # Cast explicitly: an untyped parameter lets PostgreSQL resolve
                # `created_at - $1` through `timestamptz - timestamptz -> interval`,
                # and the UPDATE then fails on the column type rather than doing
                # arithmetic.
                text(
                    "UPDATE reviews SET created_at = created_at - CAST(:d AS interval) "
                    "WHERE id = :i"
                ),
                {"d": by, "i": review_id},
            )


@pytest_asyncio.fixture
async def world(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> World:
    tag = uuid4().hex[:8]
    mentor = await make_public_mentor(db_engine, tag)
    offering_a = await add_session_type(db_engine, mentor, name=f"CV review {tag}")
    offering_b = await add_session_type(db_engine, mentor, name=f"Interview prep {tag}")

    auth_id = uuid4()
    async with db_engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Mo', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.test", "a": auth_id},
            )
        ).scalar_one()

    return World(
        engine=db_engine,
        client=api_client,
        mentor=mentor,
        mentee=mentee,
        headers=bearer(api_token(auth_id)),
        offering_a=offering_a,
        offering_b=offering_b,
    )


# --------------------------------------------------------------------------
# Writing one
# --------------------------------------------------------------------------


async def test_a_mentee_reviews_a_completed_session(world: World) -> None:
    session_id = await world.completed(world.offering_a)

    response = await world.review(session_id)

    assert response.status_code == 201
    assert response.json()["session_id"] == session_id


async def test_the_answers_come_back_as_words(world: World) -> None:
    """The column stores `3`; the contract says `excellent`.

    A magic number on the wire is a value whose meaning lives in a document,
    which is the half of settled decision #100 that still applies once the
    column has been argued out of `text`.
    """
    session_id = await world.completed(world.offering_a)

    body = (await world.review(session_id)).json()

    assert body["communication_rating"] == "excellent"
    assert body["knowledge_rating"] == "great"


async def test_the_mentor_is_taken_from_the_session_not_the_request(world: World) -> None:
    """The invariant no `CHECK` can express, enforced where it can be.

    `reviewed_for` must equal `sessions.mentor_id`, and a constraint cannot span
    two tables. Sending a different mentor must not move it — the field is not
    part of the contract at all, so it is ignored rather than honoured.
    """
    session_id = await world.completed(world.offering_a)
    stranger = await make_public_mentor(world.engine, uuid4().hex[:8])

    body = (await world.review(session_id, reviewed_for=str(stranger))).json()

    assert body["reviewed_for"] == str(world.mentor)


async def test_the_platform_feedback_is_never_published(world: World) -> None:
    """`private_review` is accepted, stored, and absent from every read model."""
    session_id = await world.completed(world.offering_a)

    body = (
        await world.review(session_id, private_review="The join button was hard to find")
    ).json()

    assert "private_review" not in body


# --------------------------------------------------------------------------
# Who may write, and about what
# --------------------------------------------------------------------------


async def test_somebody_elses_session_is_not_found(world: World) -> None:
    """`404`, not `403`: a `403` confirms the id names a real session."""
    async with world.engine.begin() as conn:
        other = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Other', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"other-{uuid4().hex[:8]}@example.test"},
            )
        ).scalar_one()
        session_id = str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, "
                        "starts_at, duration_minutes, status) "
                        "VALUES (:m, :e, :t, now() - interval '2 days', 45, 'completed') "
                        "RETURNING id"
                    ),
                    {"m": world.mentor, "e": other, "t": world.offering_a},
                )
            ).scalar_one()
        )

    assert (await world.review(session_id)).status_code == 404


async def test_a_session_that_has_not_completed_cannot_be_reviewed(world: World) -> None:
    """Same `404`. A session still to happen is not a thing there is an opinion
    about, and separating it from *not yours* would leak which ids are real."""
    session_id = await world.completed(world.offering_a)
    async with world.engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'confirmed' WHERE id = :i"), {"i": session_id}
        )

    assert (await world.review(session_id)).status_code == 404


async def test_an_unknown_session_is_not_found(world: World) -> None:
    assert (await world.review(str(uuid4()))).status_code == 404


# --------------------------------------------------------------------------
# The two refusals, and the types that tell them apart
# --------------------------------------------------------------------------


async def test_reviewing_the_same_session_twice_is_terminal(world: World) -> None:
    session_id = await world.completed(world.offering_a)
    await world.review(session_id)

    response = await world.review(session_id)

    assert response.status_code == 409
    assert response.json()["type"] == ALREADY


async def test_a_second_session_on_the_same_offering_waits(world: World) -> None:
    """The interval, and the type that says it will pass."""
    await world.review(await world.completed(world.offering_a, days_ago=1))

    response = await world.review(await world.completed(world.offering_a, days_ago=2))

    assert response.status_code == 409
    assert response.json()["type"] == TOO_SOON


async def test_a_different_offering_may_be_reviewed_straight_away(world: World) -> None:
    """**The reason the interval is per offering rather than per mentor.**

    A mentor strong at CV review and weak at interview prep is two facts. A
    mentor-wide window keeps whichever the mentee happened to book first and
    discards the other, and the second review is never even requested, because
    the request producer suppresses on this same predicate.
    """
    await world.review(await world.completed(world.offering_a, days_ago=1))

    response = await world.review(await world.completed(world.offering_b, days_ago=2))

    assert response.status_code == 201


async def test_the_terminal_refusal_wins_when_both_apply(world: World) -> None:
    """A review of this session is also a review of this offering.

    So once one exists both refusals are true, and the order they are checked in
    decides what a client is told. Reporting the retryable one would send them
    away for a month to retry something that can never succeed.
    """
    session_id = await world.completed(world.offering_a)
    await world.review(session_id)

    assert (await world.review(session_id)).json()["type"] == ALREADY


async def test_the_window_reopens_once_the_interval_has_passed(world: World) -> None:
    """The accepting half of the interval. Without it, a rule that refused
    everything would pass every test above."""
    first = (await world.review(await world.completed(world.offering_a, days_ago=1))).json()
    await world.age(first["id"], REVIEW_INTERVAL + dt.timedelta(days=1))

    response = await world.review(await world.completed(world.offering_a, days_ago=2))

    assert response.status_code == 201


async def test_a_withdrawn_review_still_holds_the_interval(world: World) -> None:
    """Withdrawal removes a review from what is published, not from what happened.

    If moderation reset the window, an author whose abusive review was taken
    down could write a replacement immediately — the incentive exactly inverted.
    """
    first = (await world.review(await world.completed(world.offering_a, days_ago=1))).json()
    async with world.engine.begin() as conn:
        await conn.execute(
            text("UPDATE reviews SET deleted_at = now() WHERE id = :i"), {"i": first["id"]}
        )

    response = await world.review(await world.completed(world.offering_a, days_ago=2))

    assert response.status_code == 409
    assert response.json()["type"] == TOO_SOON


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("communication_rating", "sublime"),
        ("valuable_rating", 6),
        ("valuable_rating", 0),
        ("nps_recommend_score", 0),
        ("nps_recommend_score", 11),
        ("public_review", ""),
        ("public_review", "   "),
    ],
)
async def test_a_body_off_its_scale_is_refused(world: World, field: str, value: Any) -> None:
    """`422`, at the boundary, before the column ever sees it.

    `"   "` is in the list because `Normalised` trims to `None` and a required
    field must then refuse it — whitespace is not a review.
    """
    session_id = await world.completed(world.offering_a)

    assert (await world.review(session_id, **{field: value})).status_code == 422


async def test_a_review_needs_a_token(world: World) -> None:
    session_id = await world.completed(world.offering_a)

    response = await world.client.post("/api/v1/reviews", json=BODY | {"session_id": session_id})

    assert response.status_code == 401


# --------------------------------------------------------------------------
# Correcting one
# --------------------------------------------------------------------------


async def test_a_typo_can_be_fixed_inside_the_window(world: World) -> None:
    review = (await world.review(await world.completed(world.offering_a))).json()

    response = await world.client.patch(
        f"/api/v1/reviews/{review['id']}",
        json={"public_review": "He showed me what a strong portfolio looks like."},
        headers=world.headers,
    )

    assert response.status_code == 200
    assert response.json()["public_review"].endswith("portfolio looks like.")


async def test_the_window_shuts(world: World) -> None:
    review = (await world.review(await world.completed(world.offering_a))).json()
    await world.age(review["id"], REVIEW_EDIT_WINDOW + dt.timedelta(minutes=1))

    response = await world.client.patch(
        f"/api/v1/reviews/{review['id']}",
        json={"public_review": "Too late."},
        headers=world.headers,
    )

    assert response.status_code == 409


async def test_an_edit_never_postpones_the_next_review(world: World) -> None:
    """**The reason both windows read `created_at`.**

    `updated_at` moves on every edit, so an interval measured from it would
    restart each time — and a mentee who corrected a typo would wait a further
    thirty days without being told why.
    """
    first = (await world.review(await world.completed(world.offering_a, days_ago=1))).json()
    await world.age(first["id"], REVIEW_INTERVAL + dt.timedelta(days=1))
    await world.client.patch(
        f"/api/v1/reviews/{first['id']}",
        json={"public_review": "Edited long after the fact."},
        headers=world.headers,
    )

    response = await world.review(await world.completed(world.offering_a, days_ago=2))

    assert response.status_code in {201, 409}, "the edit must not have changed the answer"
    assert response.status_code == 201


async def test_an_edit_cannot_move_a_review_to_another_session(world: World) -> None:
    """`session_id` is identity, not content.

    Accepting it would make the ten-minute window a way to review an ineligible
    session: write one you may, then repoint it at one you may not.
    """
    review = (await world.review(await world.completed(world.offering_a))).json()
    elsewhere = await world.completed(world.offering_b, days_ago=3)

    response = await world.client.patch(
        f"/api/v1/reviews/{review['id']}",
        json={"session_id": elsewhere},
        headers=world.headers,
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == review["session_id"]


async def test_somebody_elses_review_cannot_be_edited(world: World) -> None:
    """A **real** second user, and exactly `404`.

    A token minted for an `auth_id` with no `users` row is refused by
    `get_current_user` before `edit_review` is ever reached, so asserting
    `in {401, 404}` would pass on authentication and prove nothing about the
    scope — it would still pass with `reviewed_by` removed from the query.
    """
    review = (await world.review(await world.completed(world.offering_a))).json()
    intruder_auth = uuid4()
    async with world.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                "VALUES (:e, :a, 'Nosy', 'mentee', 'Africa/Lagos')"
            ),
            {"e": f"intruder-{uuid4().hex[:8]}@example.test", "a": intruder_auth},
        )

    response = await world.client.patch(
        f"/api/v1/reviews/{review['id']}",
        json={"public_review": "Mine now."},
        headers=bearer(api_token(intruder_auth)),
    )

    assert response.status_code == 404


async def test_a_withdrawn_review_cannot_be_edited_back(world: World) -> None:
    """The one place on this path where a soft delete changes the answer."""
    review = (await world.review(await world.completed(world.offering_a))).json()
    async with world.engine.begin() as conn:
        await conn.execute(
            text("UPDATE reviews SET deleted_at = now() WHERE id = :i"), {"i": review["id"]}
        )

    response = await world.client.patch(
        f"/api/v1/reviews/{review['id']}",
        json={"public_review": "Back from the dead."},
        headers=world.headers,
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------
# What may be reviewed — the read that makes a 409 exceptional
# --------------------------------------------------------------------------


async def test_a_completed_session_is_offered(world: World) -> None:
    session_id = await world.completed(world.offering_a)

    offered = await world.offered()

    assert [row["session_id"] for row in offered] == [session_id]
    assert offered[0]["session_type_name"].startswith("CV review")


async def test_a_reviewed_session_stops_being_offered(world: World) -> None:
    await world.review(await world.completed(world.offering_a))

    assert await world.offered() == []


async def test_the_picker_and_the_write_agree(world: World) -> None:
    """**One predicate, and this is the test that says so.**

    A list that offered a session the write then refused would be two rules for
    one question, which is the shape non-negotiable #8 calls a defect. The
    interval suppresses the second session on the same offering, so it must not
    appear here either.
    """
    await world.review(await world.completed(world.offering_a, days_ago=1))
    suppressed = await world.completed(world.offering_a, days_ago=2)
    available = await world.completed(world.offering_b, days_ago=3)

    offered = {row["session_id"] for row in await world.offered()}
    assert available in offered
    assert suppressed not in offered
    assert (await world.review(suppressed)).status_code == 409


async def test_the_list_narrows_to_one_mentor(world: World) -> None:
    mine = await world.completed(world.offering_a)
    stranger = await make_public_mentor(world.engine, uuid4().hex[:8])
    async with world.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, status) "
                "VALUES (:m, :e, now() - interval '5 days', 45, 'completed')"
            ),
            {"m": stranger, "e": world.mentee},
        )

    assert [row["session_id"] for row in await world.offered(world.mentor)] == [mine]


async def test_a_migrated_session_with_no_offering_is_still_reviewable(world: World) -> None:
    """`sessions.session_type_id` is nullable, so the interval cannot compare.

    The *one review per session* rule still caps these at one apiece, which is
    the protection that matters — and refusing them outright would make every
    migrated session permanently unreviewable.
    """
    session_id = await world.completed(None, days_ago=4)

    assert session_id in {row["session_id"] for row in await world.offered()}
    assert (await world.review(session_id)).status_code == 201


async def test_the_list_comes_back_in_the_envelope(world: World) -> None:
    """Every list endpoint returns `Page`, including one that will never page.

    `schemas/common.py` states why: a bare array has nowhere to put pagination
    metadata, so adding it later is a breaking change for every client already
    parsing the array. This endpoint shipped one for exactly one review round,
    and this test is what stops the next one.
    """
    await world.completed(world.offering_a)

    body = (await world.client.get("/api/v1/me/reviewable-sessions", headers=world.headers)).json()

    assert set(body) == {"data", "next_cursor"}
    assert body["next_cursor"] is None


async def test_two_taps_produce_one_review(world: World, db_engine: AsyncEngine) -> None:
    """**The pre-check races, and the constraint is what actually decides.**

    Both writers pass `already_reviewed` while neither has committed, so the
    check cannot be the guard on its own: the second insert blocks on
    `uq_reviews_one_per_session_author` and fails the moment the first commits.
    Untranslated, that `IntegrityError` leaves as a `500` — it is not an
    `AppError`, so nothing maps it to a status.

    A phone on a bad connection is exactly the caller that double-taps, which is
    the reasoning booking gives for requiring an idempotency key. Reviews need
    no key, because the second attempt has nothing new to say — but it still has
    to be told so in a shape a client can branch on.

    At store level rather than through two HTTP calls: the interleaving has to be
    exact, and a `gather` of two requests would assert this on a coin flip.
    """
    session_id = await world.completed(world.offering_a)
    values: dict[str, Any] = {
        "session_id": UUID(session_id),
        "communication_rating": 3,
        "knowledge_rating": 3,
        "practicality_rating": 3,
        "support_rating": 3,
        "valuable_rating": 5,
        "nps_recommend_score": 9,
        "public_review": "Twice over.",
        "private_review": None,
    }
    now = dt.datetime.now(dt.UTC)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as first, factory() as second:
        await write_review(first, world.mentee, values, now)
        racing = asyncio.create_task(write_review(second, world.mentee, dict(values), now))
        await until_blocked(db_engine)
        await first.commit()
        with pytest.raises(AlreadyReviewedError):
            await racing

    async with db_engine.begin() as conn:
        written = (
            await conn.execute(
                text("SELECT count(*) FROM reviews WHERE session_id = :s"), {"s": session_id}
            )
        ).scalar_one()

    assert written == 1


async def test_a_migrated_review_suppresses_no_other_migrated_session(world: World) -> None:
    """**The picker and the writer must agree about NULL, and this is the pin.**

    `within_interval` is handed a *column* by the picker and a *value* by the
    writer. `col == col` is never true for two nulls; `col == None` renders
    `IS NULL` and matches every offering-less prior review. Left alone, the
    second migrated session is offered by the list and refused by the write with
    `review-interval-not-elapsed` — and the population that happens to is the
    migrated sessions, the ones with no offering to have a window about.

    Two sessions, not one: with a single offering-less session there is nothing
    for the first review to suppress and the divergence is invisible.
    """
    first = await world.completed(None, days_ago=4)
    second = await world.completed(None, days_ago=6)
    assert (await world.review(first)).status_code == 201

    offered = {row["session_id"] for row in await world.offered()}
    response = await world.review(second)

    assert second in offered, "the picker offers it"
    assert response.status_code == 201, "so the writer must accept it"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_review", None),
        ("public_review", "   "),
        ("valuable_rating", None),
        ("nps_recommend_score", None),
        ("communication_rating", None),
    ],
)
async def test_an_edit_may_omit_a_field_but_not_empty_it(
    world: World, field: str, value: Any
) -> None:
    """`422` at the boundary, where the alternative is a `500` from the column.

    Every one of these is `NOT NULL`, so a null reaching the `UPDATE` raises
    `NotNullViolationError` — not an `AppError`, so nothing maps it and the
    caller gets a `500` for a request with a clean answer. Whitespace is the same
    case: `Normalised` trims it to null before validation, and `min_length`
    guards only the `str` branch of `str | None`.
    """
    review = (await world.review(await world.completed(world.offering_a))).json()

    response = await world.client.patch(
        f"/api/v1/reviews/{review['id']}", json={field: value}, headers=world.headers
    )

    assert response.status_code == 422


async def test_clearing_the_optional_feedback_is_still_allowed(world: World) -> None:
    """The counter-case, without which the guard above could refuse everything.

    `private_review` is the one genuinely nullable column, and clearing it is
    what the route means by "sending null clears it".
    """
    review = (
        await world.review(await world.completed(world.offering_a), private_review="Too long.")
    ).json()

    response = await world.client.patch(
        f"/api/v1/reviews/{review['id']}", json={"private_review": None}, headers=world.headers
    )

    assert response.status_code == 200


async def test_a_withdrawal_landing_mid_edit_wins(world: World, db_engine: AsyncEngine) -> None:
    """**The scope belongs in the UPDATE, not only in the SELECT before it.**

    Under READ COMMITTED a withdrawal can commit between the two statements, so
    an `UPDATE` keyed on the id alone would write to a review that is no longer
    editable — the row `test_a_withdrawn_review_cannot_be_edited_back` proves is
    unreachable through the normal path.

    Non-negotiable #5 puts the scope in the query on every write path, and this
    is the case that shows why that is not a formality.
    """
    review = (await world.review(await world.completed(world.offering_a))).json()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as editor:
        await editor.execute(text("SELECT id FROM reviews WHERE id = :i"), {"i": review["id"]})
        async with db_engine.begin() as moderator:
            await moderator.execute(
                text("UPDATE reviews SET deleted_at = now() WHERE id = :i"), {"i": review["id"]}
            )
        with pytest.raises(NotFoundError):
            await edit_review(
                editor,
                world.mentee,
                UUID(review["id"]),
                {"public_review": "Snuck in."},
                dt.datetime.now(dt.UTC),
            )

    async with db_engine.begin() as conn:
        stored = (
            await conn.execute(
                text("SELECT public_review FROM reviews WHERE id = :i"), {"i": review["id"]}
            )
        ).scalar_one()

    assert stored != "Snuck in."

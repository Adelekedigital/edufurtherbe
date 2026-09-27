"""`GET /me/mentors/{mentor_id}/relationship` — the caller's history with one mentor.

What the profile's review prompts are built from: "you can review after your
first session" (`completed_sessions_with_mentor == 0`), "you've had N more
sessions since your last review" (`last_reviewed_at`), and whether a review is
owed right now (`review_due`). Each figure is read through the predicate its
own endpoint already uses, so the prompt cannot disagree with the list it
points at.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from tests.integration import test_api_reviews
from tests.integration.test_api_reviews import World

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: The reviews suite's world — one mentor, two offerings, a mentee with a token —
#: reused rather than rebuilt, so the two suites cannot set up a review differently.
world = test_api_reviews.world


def url(mentor: object) -> str:
    return f"/api/v1/me/mentors/{mentor}/relationship"


async def relationship(w: World, mentor: object | None = None) -> dict[str, object]:
    response = await w.client.get(url(mentor or w.mentor), headers=w.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def session_with(w: World, status: str, *, mentee: UUID | None = None) -> None:
    async with w.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, "
                "starts_at, duration_minutes, status) "
                "VALUES (:m, :e, :t, :s, 45, :status)"
            ),
            {
                "m": w.mentor,
                "e": mentee or w.mentee,
                "t": w.offering_a,
                "s": dt.datetime.now(dt.UTC) - dt.timedelta(days=3, minutes=len(status)),
                "status": status,
            },
        )


async def another_mentee(w: World) -> UUID:
    async with w.engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO users (email, auth_id, primary_role, timezone) "
                            "VALUES (:e, :a, 'mentee', 'UTC') RETURNING id"
                        ),
                        {"e": f"other-{uuid4().hex[:8]}@example.test", "a": uuid4()},
                    )
                ).scalar_one()
            )
        )


async def test_no_history_is_zeros_not_an_error(world: World) -> None:
    body = await relationship(world)

    assert body == {
        "completed_sessions_with_mentor": 0,
        "last_reviewed_at": None,
        "review_due": False,
    }


async def test_completed_sessions_are_counted(world: World) -> None:
    await world.completed(world.offering_a, days_ago=1)
    await world.completed(world.offering_b, days_ago=2)

    body = await relationship(world)

    assert body["completed_sessions_with_mentor"] == 2
    assert body["review_due"] is True


@pytest.mark.parametrize("status", ["confirmed", "cancelled", "no_show"])
async def test_a_session_that_did_not_happen_is_not_counted(world: World, status: str) -> None:
    await session_with(world, status)

    body = await relationship(world)

    assert body["completed_sessions_with_mentor"] == 0
    assert body["review_due"] is False


async def test_another_mentees_sessions_and_reviews_are_not_mine(world: World) -> None:
    other = await another_mentee(world)
    await session_with(world, "completed", mentee=other)

    body = await relationship(world)

    assert body["completed_sessions_with_mentor"] == 0
    assert body["review_due"] is False


async def test_a_session_with_another_mentor_is_not_counted(world: World) -> None:
    await world.completed(world.offering_a)

    body = await relationship(world, uuid4())

    assert body["completed_sessions_with_mentor"] == 0
    assert body["review_due"] is False


async def test_reviewing_it_records_when_and_clears_the_prompt(world: World) -> None:
    await world.review(await world.completed(world.offering_a, days_ago=1))

    body = await relationship(world)

    assert body["completed_sessions_with_mentor"] == 1
    assert body["review_due"] is False
    reviewed = dt.datetime.fromisoformat(str(body["last_reviewed_at"]))
    assert abs(reviewed - dt.datetime.now(dt.UTC)) < dt.timedelta(minutes=5)


async def test_the_latest_review_is_the_one_reported(world: World) -> None:
    first = (await world.review(await world.completed(world.offering_a, days_ago=5))).json()
    await world.age(first["id"], dt.timedelta(days=30))
    await world.review(await world.completed(world.offering_b, days_ago=1))

    body = await relationship(world)

    reviewed = dt.datetime.fromisoformat(str(body["last_reviewed_at"]))
    assert dt.datetime.now(dt.UTC) - reviewed < dt.timedelta(days=1)


async def test_a_withdrawn_review_is_not_a_last_review(world: World) -> None:
    review = (await world.review(await world.completed(world.offering_a))).json()
    async with world.engine.begin() as conn:
        await conn.execute(
            text("UPDATE reviews SET deleted_at = now() WHERE id = :i"), {"i": review["id"]}
        )

    body = await relationship(world)

    assert body["last_reviewed_at"] is None


async def test_review_due_agrees_with_the_reviewable_list(world: World) -> None:
    """The prompt and the list it opens must never disagree."""
    await world.completed(world.offering_a, days_ago=1)

    body = await relationship(world)
    listed = await world.offered(world.mentor)

    assert body["review_due"] is (len(listed) > 0)
    assert body["review_due"] is True


async def test_it_needs_a_token(world: World) -> None:
    response = await world.client.get(url(world.mentor))

    assert response.status_code == 401


async def test_it_is_never_cached_for_somebody_else(world: World) -> None:
    response = await world.client.get(url(world.mentor), headers=world.headers)

    assert response.headers["cache-control"] == "private, no-store"


async def test_a_malformed_mentor_id_is_a_422(world: World) -> None:
    response = await world.client.get(url("not-a-uuid"), headers=world.headers)

    assert response.status_code == 422


async def test_the_mentor_asking_about_themself_sees_their_mentee_side_only(
    world: World,
) -> None:
    """Scoped to the caller *as mentee*: a mentor's own delivered sessions are
    not sessions they received from themself."""
    await world.completed(world.offering_a)
    async with world.engine.begin() as conn:
        auth_id = (
            await conn.execute(text("SELECT auth_id FROM users WHERE id = :u"), {"u": world.mentor})
        ).scalar_one()

    response = await world.client.get(url(world.mentor), headers=bearer(api_token(auth_id)))

    assert response.json()["completed_sessions_with_mentor"] == 0


async def test_another_mentees_review_is_not_my_last_review(world: World) -> None:
    """Somebody else reviewing this mentor says nothing about when *I* did."""
    auth_id = uuid4()
    async with world.engine.begin() as conn:
        other = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, primary_role, timezone) "
                    "VALUES (:e, :a, 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"reviewer-{uuid4().hex[:8]}@example.test", "a": auth_id},
            )
        ).scalar_one()
        session_id = (
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
    theirs = await world.client.post(
        "/api/v1/reviews",
        json=test_api_reviews.BODY | {"session_id": str(session_id)},
        headers=bearer(api_token(auth_id)),
    )
    assert theirs.status_code == 201, theirs.text

    body = await relationship(world)

    assert body["last_reviewed_at"] is None

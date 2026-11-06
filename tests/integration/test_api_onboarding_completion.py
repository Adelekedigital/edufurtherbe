"""``POST /api/v1/me/onboarding/completion`` — the producer the starter lacked.

Before this route, ``user_onboarding.completed_at`` was written by exactly one
thing — the ETL — so the earning model's first rung was unreachable from the
API. The tests that matter here are the two that guard a spendable resource:
**the bar is enforced server-side**, and **a retry pays nothing**.
"""

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.credits import end_of_month
from conftest import add_user, api_token, bearer, give_mentee_answers

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PATH = "/api/v1/me/onboarding/completion"


async def seed(
    engine: AsyncEngine,
    auth_id: UUID,
    *,
    profile: bool = True,
    goal: bool = True,
    mentor: bool = False,
    answers: bool = True,
) -> UUID:
    async with engine.begin() as conn:
        user_id = await add_user(
            conn,
            f"{auth_id}@example.com",
            auth_id=auth_id,
            primary_role="mentee",
            timezone="Africa/Lagos",
        )
        if profile:
            await conn.execute(
                text("INSERT INTO user_profiles (user_id, about_me) VALUES (:u, 'Hi')"),
                {"u": user_id},
            )
        if goal:
            await conn.execute(
                text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user_id}
            )
        if profile and goal and answers:
            await give_mentee_answers(conn, user_id)
        if mentor:
            await conn.execute(
                text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Mentor')"),
                {"u": user_id},
            )
        return UUID(str(user_id))


async def balance_of(engine: AsyncEngine, user_id: UUID) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT COALESCE(SUM(quantity_remaining), 0) "
                        "FROM credit_lots WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
        )


async def ledger_of(engine: AsyncEngine, user_id: UUID) -> list[Any]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT delta, reason FROM credit_transactions "
                "WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user_id},
        )
        return [(row[0], row[1]) for row in rows]


# --------------------------------------------------------------------------
# The bar — enforced here, never taken from the client
# --------------------------------------------------------------------------


async def test_a_bare_account_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The anti-farming property, asserted at the boundary.**

    The starter is granted for finishing a profile rather than for signing up,
    and the only thing making that true is this refusal. A route that recorded
    what the client asserted would hand a credit to any script that signed up.
    """
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id, profile=False, goal=False)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 409
    assert response.json()["type"] == "/problems/onboarding-incomplete"
    assert await balance_of(db_engine, user_id) == 0


async def test_a_profile_without_a_goal_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Both halves, not either — a predicate written with `or` passes every
    accepting test below."""
    auth_id = uuid4()
    await seed(db_engine, auth_id, goal=False)

    assert (await api_client.post(PATH, headers=bearer(api_token(auth_id)))).status_code == 409


async def test_a_goal_without_a_profile_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id, profile=False)

    assert (await api_client.post(PATH, headers=bearer(api_token(auth_id)))).status_code == 409


async def test_a_refusal_grants_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The refusal and the grant are one transaction; a 409 must leave no lot
    behind, and no ledger row either."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id, profile=False, goal=False)

    await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert await ledger_of(db_engine, user_id) == []


# --------------------------------------------------------------------------
# The accepting cases
# --------------------------------------------------------------------------


async def test_a_complete_mentee_is_granted_one_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 201
    assert response.json()["completed_at"] is not None
    assert await balance_of(db_engine, user_id) == 1


async def test_a_mentor_qualifies_too(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Role-appropriate, not mentee-only.** Gating on a mentee goal alone
    would refuse a mentor who finished the mentor half first.

    **Finished, but paid nothing** (decision 308): credits buy sessions, so a
    mentor with no mentee goal is at level 0, and the completion answers `200`
    — it created nothing."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id, goal=False, mentor=True)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 200
    assert response.json()["completed_at"] is not None
    assert await balance_of(db_engine, user_id) == 0


async def test_the_grant_writes_a_ledger_row(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**A balance that rose with nothing saying why is what D8 rejected.**

    The lot and its ledger entry are written together, so this asserts the row
    rather than only the balance — a writer that created the lot and forgot the
    entry passes every balance assertion in this file.
    """
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert await ledger_of(db_engine, user_id) == [(1, "grant")]


async def test_finishing_pays_level_one_as_a_top_up_dying_at_the_reset(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Decision 308: finishing is level 1, paid as a `level_top_up` that dies
    at the month end like every credit; the 1st pays the level again."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    async with db_engine.begin() as conn:
        source, expires = (
            await conn.execute(
                text("SELECT source, expires_at FROM credit_lots WHERE user_id = :u"),
                {"u": user_id},
            )
        ).one()

    assert source == "level_top_up"
    assert expires == end_of_month(dt.datetime.now(dt.UTC))


# --------------------------------------------------------------------------
# Idempotence — the guarantee that makes the producer safe to retry
# --------------------------------------------------------------------------


async def test_a_second_call_grants_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Asserted on the balance, not on the response.**

    A route that answered 200 while quietly inserting a second lot would pass
    an assertion about status codes. The database guarantee is what is under
    test, so the balance is what is checked.
    """
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    token = bearer(api_token(auth_id))

    first = await api_client.post(PATH, headers=token)
    second = await api_client.post(PATH, headers=token)

    assert first.status_code == 201
    assert second.status_code == 200
    assert await balance_of(db_engine, user_id) == 1
    assert await ledger_of(db_engine, user_id) == [(1, "grant")]


async def test_a_retry_in_a_new_month_before_the_first_pays_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Found by code review.** A finished user retrying once their month has
    turned — the 1st's job not yet run — must not be paid here: the job pays
    their level, and paying it from completion would answer `201` and send the
    first-credits email a second time."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    token = bearer(api_token(auth_id))
    assert (await api_client.post(PATH, headers=token)).status_code == 201
    # The month turned: last month's level credit has died.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE credit_lots SET expires_at = now() - interval '1 day' WHERE user_id = :u"),
            {"u": user_id},
        )

    retry = await api_client.post(PATH, headers=token)

    assert retry.status_code == 200
    assert await ledger_of(db_engine, user_id) == [(1, "grant")]
    async with db_engine.begin() as conn:
        emails = await conn.scalar(
            text(
                "SELECT count(*) FROM outbox_events "
                "WHERE event_type = 'credits_granted' AND entity_id = :u"
            ),
            {"u": user_id},
        )
    assert emails == 1


async def test_a_repeat_does_not_move_the_completion_date(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Re-finishing does not change the date somebody finished.

    Harmless today because the starter never expires — and wrong the moment any
    dated grant keys off this timestamp, which is why it is kept rather than
    overwritten.
    """
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    token = bearer(api_token(auth_id))

    first = (await api_client.post(PATH, headers=token)).json()["completed_at"]
    second = (await api_client.post(PATH, headers=token)).json()["completed_at"]

    assert first == second


async def test_a_user_the_etl_completed_still_gets_their_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Changed by decision 308: 200, and nothing paid here.** A migrated
    user the ETL marked finished is already level 1 by `completed_at`, so the
    1st pays them; completion pays only the first time it records a finish (a
    retry on the 1st would otherwise pay twice and re-send the first-credits
    email). The row's `last_step` comes back untouched."""
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_onboarding (user_id, last_step, completed_at) "
                "VALUES (:u, '6', now())"
            ),
            {"u": user_id},
        )

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.status_code == 200
    assert response.json()["last_step"] == "6"
    assert await balance_of(db_engine, user_id) == 0


async def test_the_credit_appears_on_me(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The two halves of PR 3 and PR 4 meeting: granting here shows there."""
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    token = bearer(api_token(auth_id))

    await api_client.post(PATH, headers=token)
    block = (await api_client.get("/api/v1/me", headers=token)).json()["credits"]

    assert block["balance"] == 1
    assert block["state"] == "low"


async def test_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.post(PATH)).status_code == 401


async def test_the_completion_points_at_where_the_state_lives(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A 201 that does not say where the thing went makes a client guess."""
    auth_id = uuid4()
    await seed(db_engine, auth_id)

    response = await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    assert response.headers["Location"] == "/api/v1/me/onboarding"


async def test_the_record_is_readable_afterwards(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The `Location` header has to point somewhere real."""
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    token = bearer(api_token(auth_id))

    await api_client.post(PATH, headers=token)
    body = (await api_client.get("/api/v1/me/onboarding", headers=token)).json()

    assert body["completed_at"] is not None


async def test_reading_before_starting_is_a_404(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Never having begun is a different fact from having begun and got
    nowhere, and `last_step` is null in both — so an empty record would
    conflate them."""
    auth_id = uuid4()
    await seed(db_engine, auth_id)

    response = await api_client.get("/api/v1/me/onboarding", headers=bearer(api_token(auth_id)))

    assert response.status_code == 404


async def test_a_legacy_row_with_no_completion_date_is_readable(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**A migrated user who was mid-onboarding at cutover.**

    The ETL writes `user_onboarding` rows with a null `completed_at` for anybody
    who had started and not finished. A required field on the read schema turned
    this into a 500 for exactly those users — the ones the flow most needs to
    send back to where they left off. Found by code review.

    Distinct from never having started, which is still a 404.
    """
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_onboarding (user_id, last_step, completed_at) "
                "VALUES (:u, '4', NULL)"
            ),
            {"u": user_id},
        )

    response = await api_client.get("/api/v1/me/onboarding", headers=bearer(api_token(auth_id)))

    assert response.status_code == 200
    assert response.json() == {"completed_at": None, "last_step": "4"}


async def test_the_starter_credit_is_announced(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Fired where the credit is granted, not where the account was made.**

    The template is addressed to a new user, but the credit arrives on profile
    completion — a message sent at signup would tell somebody they have a
    credit before the lot exists.
    """
    auth_id = uuid4()
    user_id = await seed(db_engine, auth_id)

    await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    async with db_engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT entity_type, payload FROM outbox_events "
                "WHERE event_type = 'credits_granted'"
            )
        )
        queued = [dict(row) for row in rows.mappings()]

    assert [row["entity_type"] for row in queued] == ["user"]
    assert queued[0]["payload"]["recipient_id"] == str(user_id)


async def test_a_retried_completion_announces_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Guarded on whether a lot was created, not on the request succeeding.**

    A retry grants nothing, so it says nothing — telling somebody twice that
    their first credit arrived is worse than a slightly late first telling.
    """
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    token = bearer(api_token(auth_id))

    await api_client.post(PATH, headers=token)
    await api_client.post(PATH, headers=token)

    async with db_engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type = 'credits_granted'")
            )
        ).scalar_one()

    assert count == 1


async def test_a_refused_completion_announces_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The enqueue is in the granting transaction, so a credit that never
    existed cannot have been announced."""
    auth_id = uuid4()
    await seed(db_engine, auth_id, profile=False, goal=False)

    await api_client.post(PATH, headers=bearer(api_token(auth_id)))

    async with db_engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type = 'credits_granted'")
            )
        ).scalar_one()

    assert count == 0

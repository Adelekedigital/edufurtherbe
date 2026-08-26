"""Invites, claiming them, and the recurring grant one of them opens.

**This gate is the stricter of the two, and the tests reflect that.** The
starter is one credit once; an unlock opens three a month indefinitely. So the
cases that matter are the ones where a second grant could slip through: a
referrer with two qualifying invitees, an invitee who finishes twice, and a
claim replayed.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

REFERRALS = "/api/v1/me/referrals"
CLAIM = "/api/v1/me/referrals/claim"
COMPLETE = "/api/v1/me/onboarding/completion"


async def seed(engine: AsyncEngine, auth_id: UUID, *, complete: bool = True) -> UUID:
    """A user who can finish onboarding, unless told otherwise."""
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, primary_role, timezone) "
                    "VALUES (:e, :a, 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"{auth_id}@example.com", "a": auth_id},
            )
        ).scalar_one()
        if complete:
            await conn.execute(
                text("INSERT INTO user_profiles (user_id, about_me) VALUES (:u, 'Hi')"),
                {"u": user_id},
            )
            await conn.execute(
                text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user_id}
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


async def sources_of(engine: AsyncEngine, user_id: UUID) -> list[str]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT source FROM credit_lots WHERE user_id = :u ORDER BY created_at"),
            {"u": user_id},
        )
        return [row[0] for row in rows]


async def unlocks_of(engine: AsyncEngine, user_id: UUID) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM referral_unlocks WHERE user_id = :u"),
                    {"u": user_id},
                )
            ).scalar_one()
        )


async def invite(client: httpx.AsyncClient, auth_id: UUID, email: str | None = None) -> Any:
    body = {"invitee_email": email} if email else {}
    return await client.post(REFERRALS, json=body, headers=bearer(api_token(auth_id)))


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def test_an_invite_is_created_with_a_code(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id)

    response = await invite(api_client, auth_id, "friend@example.com")

    assert response.status_code == 201
    assert response.headers["Location"] == REFERRALS
    assert len(response.json()["code"]) >= 22


async def test_an_invite_may_carry_no_address(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A shared link has no addressee; the code attributes the arrival."""
    auth_id = uuid4()
    await seed(db_engine, auth_id)

    assert (await invite(api_client, auth_id)).status_code == 201


async def test_inviting_the_same_address_twice_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    await invite(api_client, auth_id, "dup@example.com")

    assert (await invite(api_client, auth_id, "dup@example.com")).status_code == 409


async def test_two_referrers_may_invite_the_same_person(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The accepting case that decides the rule's shape.**

    Scoped to the address alone this is refused, which makes a referral
    programme a race to invite — and the rejecting test above passes either way.
    """
    first, second = uuid4(), uuid4()
    await seed(db_engine, first)
    await seed(db_engine, second)
    await invite(api_client, first, "popular@example.com")

    assert (await invite(api_client, second, "popular@example.com")).status_code == 201


async def test_invites_are_listed_newest_first(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    await seed(db_engine, auth_id)
    await invite(api_client, auth_id, "one@example.com")
    await invite(api_client, auth_id, "two@example.com")

    body = (await api_client.get(REFERRALS, headers=bearer(api_token(auth_id)))).json()

    assert [row["invitee_email"] for row in body] == ["two@example.com", "one@example.com"]


async def test_the_list_shows_only_your_own(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Object-level scoping, in the query rather than after it."""
    mine, theirs = uuid4(), uuid4()
    await seed(db_engine, mine)
    await seed(db_engine, theirs)
    await invite(api_client, theirs, "not-yours@example.com")

    body = (await api_client.get(REFERRALS, headers=bearer(api_token(mine)))).json()

    assert body == []


# --------------------------------------------------------------------------
# Claiming
# --------------------------------------------------------------------------


async def test_claiming_attaches_the_invitee(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, invitee = uuid4(), uuid4()
    await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]

    response = await api_client.post(CLAIM, json={"code": code}, headers=bearer(api_token(invitee)))

    assert response.status_code == 200
    assert response.json()["signed_up_at"] is not None


async def test_claiming_your_own_invite_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The cheapest possible farm**, and the gate it opens is recurring.

    The database CHECK is the guarantee; this asserts the caller learns it as a
    409 rather than a 500.
    """
    referrer = uuid4()
    await seed(db_engine, referrer)
    code = (await invite(api_client, referrer, "self@example.com")).json()["code"]

    response = await api_client.post(
        CLAIM, json={"code": code}, headers=bearer(api_token(referrer))
    )

    assert response.status_code == 409


async def test_claiming_someone_elses_claim_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, first, second = uuid4(), uuid4(), uuid4()
    await seed(db_engine, referrer)
    await seed(db_engine, first)
    await seed(db_engine, second)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]
    await api_client.post(CLAIM, json={"code": code}, headers=bearer(api_token(first)))

    response = await api_client.post(CLAIM, json={"code": code}, headers=bearer(api_token(second)))

    assert response.status_code == 409


async def test_claiming_twice_yourself_is_idempotent(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The front end holds the code across sign-up; a retry must not read as an
    error."""
    referrer, invitee = uuid4(), uuid4()
    await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]
    token = bearer(api_token(invitee))

    first = await api_client.post(CLAIM, json={"code": code}, headers=token)
    second = await api_client.post(CLAIM, json={"code": code}, headers=token)

    assert second.status_code == 200
    assert second.json()["signed_up_at"] == first.json()["signed_up_at"]


async def test_an_unknown_code_is_a_404(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    invitee = uuid4()
    await seed(db_engine, invitee)

    response = await api_client.post(
        CLAIM, json={"code": "no-such-code"}, headers=bearer(api_token(invitee))
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Qualifying — where the recurring grant is actually paid
# --------------------------------------------------------------------------


async def test_finishing_onboarding_pays_the_referrer(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The earning model's second rung.**

    Claiming pays nothing; finishing the profile does. That separation is the
    abuse boundary — signing up and vanishing unlocks nothing.
    """
    referrer, invitee = uuid4(), uuid4()
    referrer_id = await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]
    token = bearer(api_token(invitee))
    await api_client.post(CLAIM, json={"code": code}, headers=token)

    assert await balance_of(db_engine, referrer_id) == 0

    await api_client.post(COMPLETE, headers=token)

    assert await balance_of(db_engine, referrer_id) == 2
    assert await sources_of(db_engine, referrer_id) == ["referral_unlock"]
    assert await unlocks_of(db_engine, referrer_id) == 1


async def test_claiming_alone_pays_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The rejecting half of the rule above, asserted on the balance."""
    referrer, invitee = uuid4(), uuid4()
    referrer_id = await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]

    await api_client.post(CLAIM, json={"code": code}, headers=bearer(api_token(invitee)))

    assert await balance_of(db_engine, referrer_id) == 0
    assert await unlocks_of(db_engine, referrer_id) == 0


async def test_a_second_qualifying_invitee_pays_nothing_more(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The case that costs money if it is wrong.**

    The gate is once-only: it opens a floor, and opening a floor that is already
    open must not pay again. Enforced by `uq_referral_unlocks_user_id` rather
    than by reading first, because two invitees finishing at once is exactly the
    race a read-then-write loses.
    """
    referrer, first, second = uuid4(), uuid4(), uuid4()
    referrer_id = await seed(db_engine, referrer)
    await seed(db_engine, first)
    await seed(db_engine, second)

    for auth_id, email in ((first, "a@example.com"), (second, "b@example.com")):
        code = (await invite(api_client, referrer, email)).json()["code"]
        token = bearer(api_token(auth_id))
        await api_client.post(CLAIM, json={"code": code}, headers=token)
        await api_client.post(COMPLETE, headers=token)

    assert await balance_of(db_engine, referrer_id) == 2
    assert await unlocks_of(db_engine, referrer_id) == 1


async def test_the_second_invite_is_still_marked_qualified(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """It genuinely qualified, and the record should say so even though the
    floor it would have opened is already open."""
    referrer, first, second = uuid4(), uuid4(), uuid4()
    await seed(db_engine, referrer)
    await seed(db_engine, first)
    await seed(db_engine, second)

    for auth_id, email in ((first, "a@example.com"), (second, "b@example.com")):
        code = (await invite(api_client, referrer, email)).json()["code"]
        token = bearer(api_token(auth_id))
        await api_client.post(CLAIM, json={"code": code}, headers=token)
        await api_client.post(COMPLETE, headers=token)

    body = (await api_client.get(REFERRALS, headers=bearer(api_token(referrer)))).json()

    assert all(row["qualified_at"] is not None for row in body)


async def test_the_invitee_finishing_twice_does_not_move_qualified_at(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Asserted on the date, not the balance — and that distinction is the
    whole test.**

    Onboarding completion is idempotent, so qualification is asked twice for the
    same referral. An earlier version of this test checked only that the
    referrer still had two credits, and it *passed with the*
    ``already_qualified`` *predicate removed*: the unlock's `UNIQUE` refuses the
    second row, so no second grant happens either way.

    What the predicate alone protects is `qualified_at`. Without it the repeat
    rewrites the moment the invite qualified — which is harmless today and wrong
    the moment anything keys off it, and invisible to every balance assertion.
    """
    referrer, invitee = uuid4(), uuid4()
    referrer_id = await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]
    token = bearer(api_token(invitee))
    await api_client.post(CLAIM, json={"code": code}, headers=token)
    referrer_token = bearer(api_token(referrer))

    await api_client.post(COMPLETE, headers=token)
    first = (await api_client.get(REFERRALS, headers=referrer_token)).json()[0]["qualified_at"]
    await api_client.post(COMPLETE, headers=token)
    second = (await api_client.get(REFERRALS, headers=referrer_token)).json()[0]["qualified_at"]

    assert first == second
    assert await balance_of(db_engine, referrer_id) == 2


async def test_finishing_without_a_claim_pays_nobody(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Most users have no referral at all; qualification must be a no-op then."""
    invitee = uuid4()
    invitee_id = await seed(db_engine, invitee)

    response = await api_client.post(COMPLETE, headers=bearer(api_token(invitee)))

    assert response.status_code == 201
    assert await sources_of(db_engine, invitee_id) == ["profile_completed"]


async def test_the_unlock_grant_expires_at_the_reset(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Unlike the starter, these two are perishable — the ladder is one
    non-expiring credit plus everything else dying at the reset."""
    referrer, invitee = uuid4(), uuid4()
    referrer_id = await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]
    token = bearer(api_token(invitee))
    await api_client.post(CLAIM, json={"code": code}, headers=token)
    await api_client.post(COMPLETE, headers=token)

    async with db_engine.begin() as conn:
        expires = (
            await conn.execute(
                text(
                    "SELECT expires_at FROM credit_lots "
                    "WHERE user_id = :u AND source = 'referral_unlock'"
                ),
                {"u": referrer_id},
            )
        ).scalar_one()

    assert expires is not None
    assert expires.day == 1


async def test_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.post(REFERRALS, json={})).status_code == 401
    assert (await api_client.get(REFERRALS)).status_code == 401
    assert (await api_client.post(CLAIM, json={"code": "x"})).status_code == 401


async def test_claiming_a_second_different_invite_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**This locked users out permanently before it was refused.**

    Both claims used to answer 200. The next `POST /onboarding/completion` then
    ran `qualify_invitee`, whose `.one_or_none()` raised `MultipleResultsFound`
    — not an `AppError`, so a 500 that rolled the transaction back and recurred
    on every retry. Onboarding could never be finished, the starter credit never
    arrived, and there was no way to un-claim.

    `uq_referrals_invitee` is the guarantee; the 409 is what makes it something
    the caller can act on.
    """
    first, second, invitee = uuid4(), uuid4(), uuid4()
    await seed(db_engine, first)
    await seed(db_engine, second)
    await seed(db_engine, invitee)
    token = bearer(api_token(invitee))
    one = (await invite(api_client, first, "a@example.com")).json()["code"]
    two = (await invite(api_client, second, "b@example.com")).json()["code"]

    assert (await api_client.post(CLAIM, json={"code": one}, headers=token)).status_code == 200

    assert (await api_client.post(CLAIM, json={"code": two}, headers=token)).status_code == 409


async def test_the_lockout_case_can_still_finish_onboarding(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The accepting half, and the one that matters.

    Refusing the second claim is only worth anything if the user can then get
    on with it — so this asserts the completion the old behaviour made
    unreachable.
    """
    first, second, invitee = uuid4(), uuid4(), uuid4()
    first_id = await seed(db_engine, first)
    await seed(db_engine, second)
    invitee_id = await seed(db_engine, invitee)
    token = bearer(api_token(invitee))
    one = (await invite(api_client, first, "a@example.com")).json()["code"]
    two = (await invite(api_client, second, "b@example.com")).json()["code"]
    await api_client.post(CLAIM, json={"code": one}, headers=token)
    await api_client.post(CLAIM, json={"code": two}, headers=token)

    done = await api_client.post(COMPLETE, headers=token)

    assert done.status_code == 201
    assert await balance_of(db_engine, invitee_id) == 1
    assert await balance_of(db_engine, first_id) == 2


async def test_claiming_after_finishing_still_pays_the_referrer(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The natural first run, which used to pay nobody.**

    Qualification normally fires from `complete_onboarding`, which assumes the
    claim came first. It often will not: a new user signs in, completes the
    profile the app puts in front of them, and pastes the invite code
    afterwards. In that order the claim set `signed_up_at` and nothing revisited
    the row — `qualified_at` stayed null forever, the referrer was never paid,
    and no second completion was coming to fix it. Found by code review.
    """
    referrer, invitee = uuid4(), uuid4()
    referrer_id = await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]
    token = bearer(api_token(invitee))

    # Completion first — the reversed order.
    await api_client.post(COMPLETE, headers=token)
    assert await balance_of(db_engine, referrer_id) == 0

    await api_client.post(CLAIM, json={"code": code}, headers=token)

    assert await balance_of(db_engine, referrer_id) == 2
    assert await unlocks_of(db_engine, referrer_id) == 1


async def test_claiming_before_finishing_is_unchanged(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The accepting half: the ordinary order must still pay exactly once, not
    twice now that both paths can qualify."""
    referrer, invitee = uuid4(), uuid4()
    referrer_id = await seed(db_engine, referrer)
    await seed(db_engine, invitee)
    code = (await invite(api_client, referrer, "friend@example.com")).json()["code"]
    token = bearer(api_token(invitee))

    await api_client.post(CLAIM, json={"code": code}, headers=token)
    await api_client.post(COMPLETE, headers=token)

    assert await balance_of(db_engine, referrer_id) == 2
    assert await unlocks_of(db_engine, referrer_id) == 1

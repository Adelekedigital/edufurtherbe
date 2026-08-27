"""The credit block on ``GET /api/v1/me``.

**The card is the contract.** It shows a number, a bar with a filled position, a
band, and a reset date — so the things asserted here are the things the screen
renders. What the server must never publish is a percentage: the client draws
the bar, and a third representation of one fact is non-negotiable #8.

The read filters ``expires_at`` itself rather than trusting the expiry job. That
is decision 17, and it is asserted here because a night the job did not run must
leave the balance *right* and the ledger late — never the other way round.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
LONG_PAST = datetime(2020, 1, 1, tzinfo=UTC)


async def seed_mentee(engine: AsyncEngine, auth_id: UUID, *, with_goal: bool = True) -> UUID:
    """A mentee with a goal, which is the predicate the card is gated on."""
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
        if with_goal:
            await conn.execute(
                text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user_id}
            )
        return UUID(str(user_id))


async def grant(
    engine: AsyncEngine,
    user_id: UUID,
    *,
    quantity: int = 3,
    remaining: int | None = None,
    source: str = "monthly_free",
    expires: datetime | None = FAR_FUTURE,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO credit_lots "
                "(user_id, source, quantity_granted, quantity_remaining, expires_at) "
                "VALUES (:u, :s, :g, :r, :e)"
            ),
            {
                "u": user_id,
                "s": source,
                "g": quantity,
                "r": quantity if remaining is None else remaining,
                "e": expires,
            },
        )


async def credits_of(client: httpx.AsyncClient, auth_id: UUID) -> Any:
    body = (await client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()
    return body["credits"]


async def test_a_mentee_with_no_lots_reads_zero(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**``SUM`` over no rows is NULL, not zero.**

    Every mentee before their first grant is in this state, and a null reaching
    the band function raises while a null reaching the card renders nothing.
    """
    auth_id = uuid4()
    await seed_mentee(db_engine, auth_id)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 0
    assert block["state"] == "exhausted"
    assert block["allowance"] == 4


async def test_a_funded_mentee_reads_their_balance(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=3)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 3
    assert block["state"] == "moderate"
    assert block["allowance"] == 4


async def test_a_partly_spent_lot_counts_only_what_is_left(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """``quantity_granted`` is history; the balance is ``quantity_remaining``.

    Summing the wrong column hands every user their full monthly grant back
    however much they have spent — and it reads perfectly plausibly.
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=3, remaining=1)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 1
    assert block["state"] == "low"


async def test_an_expired_lot_is_not_spendable(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Decision 17, and the half that must not wait for the job.**

    The expiry sweep writes the ``lot_expired`` ledger row. If it also decided
    what was spendable, a night it did not run would let somebody book with a
    credit that died last month. The read filters independently.
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=3, expires=LONG_PAST)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 0
    assert block["state"] == "exhausted"


async def test_a_never_expiring_lot_is_always_spendable(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The accepting half. ``expires_at IS NULL`` means never, and a filter
    written as ``expires_at > now()`` alone drops the starter entirely — which
    is the one lot every user has."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=1, source="profile_completed", expires=None)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 1


async def test_the_steady_state_is_four(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The non-expiring starter plus the monthly three — what a settled,
    unlocked mentee sees on the 1st."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=1, source="profile_completed", expires=None)
    await grant(db_engine, user_id, quantity=3)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 4
    assert block["state"] == "on_track"
    assert block["allowance"] == 4


async def test_a_balance_above_the_allowance_raises_the_allowance(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The bar cannot draw more segments than it has.**

    A refund landing after the monthly grant pushes the balance past three; the
    card would otherwise read "5 credits left" beside a three-position bar.
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=3)
    await grant(db_engine, user_id, quantity=2, source="refund")

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 5
    assert block["allowance"] == 5
    assert block["state"] == "on_track"


async def test_the_next_reset_is_the_first_of_the_next_month(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Exclusive, and the card renders it verbatim as "Next reset date"."""
    auth_id = uuid4()
    await seed_mentee(db_engine, auth_id)

    reset = datetime.fromisoformat((await credits_of(api_client, auth_id))["next_reset_at"])
    now = datetime.now(UTC)

    assert reset.day == 1
    assert reset > now
    assert (reset - now) <= timedelta(days=32)


async def test_a_user_without_a_mentee_goal_gets_no_card(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The predicate is having a mentee goal, not "not being a mentor".**

    Authorization here is profile existence, so a dual-role user is both — a
    negative predicate would hide the card from somebody who can book.
    """
    auth_id = uuid4()
    await seed_mentee(db_engine, auth_id, with_goal=False)

    assert await credits_of(api_client, auth_id) is None


async def test_the_block_carries_exactly_four_fields(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """No percentage. The client draws the bar, and a third representation of
    one fact is the first thing to drift."""
    auth_id = uuid4()
    await seed_mentee(db_engine, auth_id)

    assert set(await credits_of(api_client, auth_id)) == {
        "balance",
        "allowance",
        "state",
        "next_reset_at",
    }


# --------------------------------------------------------------------------
# The migrated user, which is who the card renders for on day one
# --------------------------------------------------------------------------


async def test_a_migrated_balance_renders_on_the_card(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The ETL's Definition of Done, asserted through the screen it is for.**

    Every other test here grants a lot the API itself would write. This one
    writes what `CreditLoader` writes — source `opening_balance`, expiring end of
    the cutover month — and checks the card can render it, because the loader
    landing correct rows in a table nobody reads correctly is not the same as the
    migration working.

    Five is the legacy maximum and 29 of 43 dev users hold it, so it is also the
    case that decides whether `allowance_for` raises: `STEADY_STATE` is four, and
    a migrated user arrives *above* it.
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    end_of_cutover_month = datetime(2026, 10, 1, tzinfo=UTC)
    await grant(
        db_engine,
        user_id,
        quantity=5,
        source="opening_balance",
        expires=end_of_cutover_month,
    )

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 5
    # **The bar has to be able to draw five.** Clamping to the steady state of
    # four would render "5 credits left" beside four positions — which is why
    # `allowance_for` publishes `max(STEADY_STATE, balance)`.
    assert block["allowance"] == 5
    assert block["state"] == "on_track"


async def test_a_migrated_balance_disappears_when_its_month_ends(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Register 5, made visible.** Not a bug — the composition of three
    decisions that are each correct — but the card is where a mentee meets it,
    and the number it shows the morning after cutover month ends is zero.

    Asserted so the behaviour is recorded rather than discovered. If the cliff is
    resolved by grandfathering migrated users into the monthly grant, this test
    is the one that should change, and it should change deliberately.
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=5, source="opening_balance", expires=LONG_PAST)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 0
    assert block["state"] == "exhausted"

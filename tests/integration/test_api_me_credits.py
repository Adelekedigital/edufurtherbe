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

from app.core.config import Settings
from app.infra.storage.supabase import SupabaseStorage
from conftest import add_qualified_invite, add_user, api_token, bearer, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
LONG_PAST = datetime(2020, 1, 1, tzinfo=UTC)


async def seed_mentee(engine: AsyncEngine, auth_id: UUID, *, with_goal: bool = True) -> UUID:
    """A mentee, with a goal unless told otherwise."""
    async with engine.begin() as conn:
        user_id = await add_user(
            conn,
            f"{auth_id}@example.com",
            auth_id=auth_id,
            primary_role="mentee",
            timezone="Africa/Lagos",
        )
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


async def onboard(engine: AsyncEngine, user_id: UUID) -> None:
    """Finished onboarding: credit level 1 (decision 308)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_onboarding (user_id, completed_at) VALUES (:u, now())"),
            {"u": user_id},
        )


async def unlock(engine: AsyncEngine, user_id: UUID) -> None:
    """The unlock: credit level 3."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id) VALUES (:u, NULL)"
            ),
            {"u": user_id},
        )


async def test_a_mentee_with_no_lots_reads_zero(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**``SUM`` over no rows is NULL, not zero.**

    Every mentee before their first grant is in this state, and a null reaching
    the band function raises while a null reaching the card renders nothing.
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await onboard(db_engine, user_id)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 0
    assert block["state"] == "exhausted"
    # The bar is the caller's level: level 1.
    assert block["allowance"] == 1


async def test_a_funded_mentee_reads_their_balance(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await unlock(db_engine, user_id)
    await grant(db_engine, user_id, quantity=2)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 2
    assert block["state"] == "moderate"
    assert block["allowance"] == 3


async def test_a_level_zero_mentee_reads_the_bar_of_level_one(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Not onboarded is level 0 (decision 308), but the card draws level 1's
    bar — the first thing they can earn — so `allowance` and `ceiling` are never
    a zero denominator (found by code review)."""
    auth_id = uuid4()
    await seed_mentee(db_engine, auth_id)

    block = await credits_of(api_client, auth_id)

    assert (block["balance"], block["allowance"], block["state"]) == (0, 1, "exhausted")
    assert block["monthly"]["ceiling"] == 1


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


async def test_a_full_level_three_month_is_on_track(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """What an unlocked mentee sees on the 1st (decision 308): three of three."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await unlock(db_engine, user_id)
    await grant(db_engine, user_id, quantity=3)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 3
    assert block["state"] == "on_track"
    assert block["allowance"] == 3


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


async def test_a_mentee_without_a_goal_still_gets_the_card(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Every caller gets the card.** Booking needs no goal and no role, so
    a mentee who skipped onboarding spends credits and must see them; a mentor
    who books does too (`test_api_me_pending_bookings`).
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id, with_goal=False)
    await grant(db_engine, user_id, quantity=2)

    card = await credits_of(api_client, auth_id)

    assert card is not None
    assert card["balance"] == 2


async def test_the_block_carries_exactly_its_fields(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """No percentage. The client draws the bar, and a third representation of
    one fact is the first thing to drift. `monthly` and `bonus` split the
    balance; they are not another measure of it."""
    auth_id = uuid4()
    await seed_mentee(db_engine, auth_id)

    block = await credits_of(api_client, auth_id)

    assert set(block) == {
        "balance",
        "allowance",
        "state",
        "next_reset_at",
        "monthly",
        "bonus",
    }
    assert set(block["monthly"]) == {"balance", "ceiling", "expires_at", "unlocked", "level"}
    assert set(block["bonus"]) == {"balance", "groups"}


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
    case that decides whether `allowance_for` raises: a migrated user can arrive
    *above* their level. (The ETL caps at the monthly grant now; five here is
    the unclamped shape the card must still draw.)
    """
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    # Relative to now, not a literal: a fixed "end of the cutover month" expired
    # on 2026-10-01 and turned this test red for every run after it.
    now = datetime.now(UTC)
    end_of_cutover_month = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    await grant(
        db_engine,
        user_id,
        quantity=5,
        source="opening_balance",
        expires=end_of_cutover_month,
    )

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 5
    # **The bar has to be able to draw five.** Clamping to the level would
    # render "5 credits left" beside fewer positions — which is why
    # `allowance_for` publishes `max(level, balance)`.
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


# --------------------------------------------------------------------------
# Monthly and bonus credits (decision 232, #344)
# --------------------------------------------------------------------------


async def test_a_starter_lot_is_a_level_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Decision 308: the starter is level 1's credit, on the monthly bar."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await onboard(db_engine, user_id)
    await grant(db_engine, user_id, quantity=1, source="profile_completed")

    block = await credits_of(api_client, auth_id)

    assert block["monthly"]["balance"] == 1
    assert block["monthly"]["ceiling"] == 1
    assert block["monthly"]["unlocked"] is False
    assert block["bonus"] == {"balance": 0, "groups": []}


async def test_a_full_month_and_a_support_grant_read_three_of_three_plus_one(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A support grant is outside the level: the bar reads full, and the extra
    sits in `bonus`."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await unlock(db_engine, user_id)
    await grant(db_engine, user_id, quantity=3, source="monthly_free")
    await grant(db_engine, user_id, quantity=1, source="admin_grant")

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 4
    assert (block["monthly"]["balance"], block["monthly"]["ceiling"]) == (3, 3)
    assert block["monthly"]["expires_at"] is not None
    assert block["bonus"]["balance"] == 1
    assert block["allowance"] == 4


async def test_support_grants_group_by_expiry_soonest_first(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    sooner = datetime.now(UTC) + timedelta(days=10)
    await grant(db_engine, user_id, quantity=1, source="admin_grant", expires=FAR_FUTURE)
    await grant(db_engine, user_id, quantity=2, source="admin_grant", expires=sooner)

    groups = (await credits_of(api_client, auth_id))["bonus"]["groups"]

    assert [g["count"] for g in groups] == [2, 1]
    assert datetime.fromisoformat(groups[0]["expires_at"]) < datetime.fromisoformat(
        groups[1]["expires_at"]
    )


async def test_a_migrated_opening_balance_is_monthly(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=2, source="opening_balance")

    block = await credits_of(api_client, auth_id)

    assert (block["monthly"]["balance"], block["bonus"]["balance"]) == (2, 0)


async def test_expired_credits_are_in_neither_part(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await unlock(db_engine, user_id)
    await grant(db_engine, user_id, quantity=3, source="monthly_free", expires=LONG_PAST)
    await grant(db_engine, user_id, quantity=1, source="admin_grant", expires=LONG_PAST)

    block = await credits_of(api_client, auth_id)

    assert block["balance"] == 0
    assert block["monthly"] == {
        "balance": 0,
        "ceiling": 3,
        "expires_at": None,
        "unlocked": True,
        "level": 3,
    }
    assert block["bonus"] == {"balance": 0, "groups": []}


async def test_the_two_parts_always_add_up_to_the_balance(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Pinned against the published balance, which a different query sums."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await grant(db_engine, user_id, quantity=3, remaining=1, source="monthly_free")
    await grant(db_engine, user_id, quantity=1, source="profile_completed", expires=None)
    await grant(db_engine, user_id, quantity=2, source="admin_grant")
    await grant(db_engine, user_id, quantity=5, source="referral_unlock", expires=LONG_PAST)

    block = await credits_of(api_client, auth_id)

    assert block["monthly"]["balance"] + block["bonus"]["balance"] == block["balance"] == 4


# --------------------------------------------------------------------------
# Whether the monthly grant is unlocked (decision 232, frontend #158)
# --------------------------------------------------------------------------


async def test_a_mentee_with_a_goal_and_an_unlock_reads_unlocked(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await unlock(db_engine, user_id)

    assert (await credits_of(api_client, auth_id))["monthly"]["unlocked"] is True


async def test_a_mentee_with_a_goal_but_no_unlock_reads_locked(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """An onboarded mentee without the unlock is at level 1 (decision 308):
    `ceiling` is their level, and `unlocked` says they have not reached 3."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id)
    await onboard(db_engine, user_id)

    monthly = (await credits_of(api_client, auth_id))["monthly"]

    assert monthly["unlocked"] is False
    assert monthly["ceiling"] == 1


async def test_an_unlock_without_a_goal_reads_locked(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id, with_goal=False)
    await unlock(db_engine, user_id)

    assert (await credits_of(api_client, auth_id))["monthly"]["unlocked"] is False


@pytest.mark.parametrize(
    ("with_goal", "unlocked"),
    [(True, True), (True, False), (False, True), (False, False)],
)
async def test_unlocked_is_exactly_who_the_monthly_grant_pays(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, with_goal: bool, unlocked: bool
) -> None:
    """**The card and the job read one rule.** Run the real grant, then compare:
    a user it paid reads `unlocked: true`, and a user it skipped reads `false`."""
    from app.core.config import Settings
    from app.domain.credits import credit_ladder
    from app.infra.db.credit_grants import grant_monthly_credits
    from app.infra.db.engine import create_session_factory

    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id, with_goal=with_goal)
    if unlocked:
        await unlock(db_engine, user_id)
    factory = create_session_factory(db_engine)
    async with factory() as db:
        await grant_monthly_credits(
            db, now=datetime.now(UTC), ladder=credit_ladder(Settings(_env_file=None))
        )
        await db.commit()

    monthly = (await credits_of(api_client, auth_id))["monthly"]

    assert monthly["unlocked"] is (monthly["balance"] > 0)
    assert monthly["unlocked"] is (with_goal and unlocked)


# --------------------------------------------------------------------------
# The raw level (decision 308): `ceiling` is floored at 1, `level` is not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "onboarded", "invited", "unlocked", "level"),
    [
        # Not onboarded: the card draws ceiling 1, but the level is 0.
        (True, False, False, False, 0),
        # A mentor-only account: no mentee goal, so level 0 whatever else holds.
        (False, True, True, False, 0),
        (True, True, False, False, 1),
        (True, True, True, False, 2),
        (True, True, True, True, 3),
        # Grandfathered from the migration: an unlock with no onboarding row.
        (True, False, False, True, 3),
    ],
)
async def test_the_level_is_published_unfloored(
    db_engine: AsyncEngine,
    api_client: httpx.AsyncClient,
    *,
    goal: bool,
    onboarded: bool,
    invited: bool,
    unlocked: bool,
    level: int,
) -> None:
    """`monthly.level` is `level_allowance` as the grants pay it, so a client
    can tell level 0 from level 1; `ceiling` keeps its floor of 1."""
    auth_id = uuid4()
    user_id = await seed_mentee(db_engine, auth_id, with_goal=goal)
    if onboarded:
        await onboard(db_engine, user_id)
    if invited:
        async with db_engine.begin() as conn:
            await add_qualified_invite(conn, user_id)
    if unlocked:
        await unlock(db_engine, user_id)

    monthly = (await credits_of(api_client, auth_id))["monthly"]

    assert monthly["level"] == level
    assert monthly["ceiling"] == max(level, 1)


async def test_the_level_is_the_rung_on_a_non_default_ladder(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """**Found by GitHub review on #576.** With `CREDIT_MONTHLY_ALLOWANCE=5`
    an unlocked mentee is paid 5 — `ceiling` 5 — and is still level 3. Built
    from the amount, `level` read 5, broke its own 0 to 3 bound and `/me`
    answered 500."""
    app = build_api_app(
        db_engine, api_storage, Settings(_env_file=None, credit_monthly_allowance=5)
    )
    async with client_for(app) as client:
        auth_id = uuid4()
        user_id = await seed_mentee(db_engine, auth_id)
        await unlock(db_engine, user_id)

        response = await client.get("/api/v1/me", headers=bearer(api_token(auth_id)))

        assert response.status_code == 200, response.text
        monthly = response.json()["credits"]["monthly"]
        assert (monthly["level"], monthly["ceiling"]) == (3, 5)

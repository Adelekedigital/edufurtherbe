"""Only a new account may claim an invite (owner, 2026-10-17; decision 264).

New is ``users.created_at`` within ``CLAIM_WINDOW`` of the database's ``now()``.
Ages are set in SQL against the same clock, so no test compares two clocks.
The refusal is a claim that would attach the caller now; an invite the caller
already holds is still returned, and the other refusals keep their answers.
"""

from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Interval, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_invite_links import (
    claim,
    link_code,
    scalar,
    user_id_of,
    users,
)
from tests.integration.test_api_referrals import invite

from app.core.config import Settings
from app.core.errors import InviteForNewAccountsError
from app.domain.credits import credit_ladder
from app.domain.referrals import CLAIM_WINDOW
from app.infra.db.referral_writer import claim_referral

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

NEW_ACCOUNTS = "/problems/invite-for-new-accounts"
JUST_OVER = CLAIM_WINDOW + timedelta(minutes=1)
JUST_UNDER = CLAIM_WINDOW - timedelta(minutes=1)
MONTH = timedelta(days=30)
#: A migrated account: its Bubble creation date (decision 29).
YEARS = timedelta(days=3 * 365)

AGE = text("UPDATE users SET created_at = now() - :age WHERE id = :u").bindparams(
    bindparam("age", type_=Interval)
)


async def age(engine: AsyncEngine, auth_id: UUID, interval: timedelta) -> None:
    """Make the account ``interval`` old on the database's clock."""
    user_id = await user_id_of(engine, auth_id)
    async with engine.begin() as conn:
        await conn.execute(AGE, {"age": interval, "u": user_id})


async def referrals_held_by(engine: AsyncEngine, auth_id: UUID) -> int:
    return int(
        await scalar(
            engine,
            "SELECT count(*) FROM referrals r JOIN users u ON u.id = r.invitee_user_id "
            "WHERE u.auth_id = :a",
            a=auth_id,
        )
    )


async def single_use_code(client: httpx.AsyncClient, referrer: UUID) -> str:
    return str((await invite(client, referrer)).json()["code"])


def refused_as_old(response: httpx.Response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["type"] == NEW_ACCOUNTS


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


async def test_an_account_just_inside_the_window_claims_a_single_use_code(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, invitee = await users(db_engine, 2)
    code = await single_use_code(api_client, referrer)
    await age(db_engine, invitee, JUST_UNDER)

    response = await claim(api_client, invitee, code)

    assert response.status_code == 200
    assert response.json()["signed_up_at"] is not None
    assert await referrals_held_by(db_engine, invitee) == 1


async def test_an_account_just_inside_the_window_claims_a_link(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2)
    code = await link_code(api_client, owner)
    await age(db_engine, arrival, JUST_UNDER)

    response = await claim(api_client, arrival, code)

    assert response.status_code == 200
    assert await referrals_held_by(db_engine, arrival) == 1


async def test_an_account_just_past_the_window_is_refused_a_single_use_code(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, invitee = await users(db_engine, 2)
    code = await single_use_code(api_client, referrer)
    await age(db_engine, invitee, JUST_OVER)

    refused_as_old(await claim(api_client, invitee, code))

    # Nothing attached: the invite is still open for somebody new.
    assert await referrals_held_by(db_engine, invitee) == 0
    assert (
        await scalar(
            db_engine,
            "SELECT invitee_user_id IS NULL AND signed_up_at IS NULL FROM referrals "
            "WHERE code = :c",
            c=code,
        )
        is True
    )


async def test_an_account_just_past_the_window_is_refused_a_link(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2)
    code = await link_code(api_client, owner)
    await age(db_engine, arrival, JUST_OVER)

    refused_as_old(await claim(api_client, arrival, code))

    # No referral row was written for the link's owner.
    assert await referrals_held_by(db_engine, arrival) == 0
    owner_id = await user_id_of(db_engine, owner)
    assert (
        await scalar(db_engine, "SELECT count(*) FROM referrals WHERE referrer_id = :o", o=owner_id)
        == 0
    )


async def test_a_migrated_account_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Migrated users carry their Bubble creation date (decision 29)."""
    referrer, owner, migrated = await users(db_engine, 3)
    code = await single_use_code(api_client, referrer)
    link = await link_code(api_client, owner)
    await age(db_engine, migrated, YEARS)

    refused_as_old(await claim(api_client, migrated, code))
    refused_as_old(await claim(api_client, migrated, link))
    assert await referrals_held_by(db_engine, migrated) == 0


# --------------------------------------------------------------------------
# Idempotence survives the window
# --------------------------------------------------------------------------


async def test_an_old_account_rereading_its_single_use_invite_gets_it(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, invitee = await users(db_engine, 2)
    code = await single_use_code(api_client, referrer)
    first = await claim(api_client, invitee, code)
    await age(db_engine, invitee, MONTH)

    again = await claim(api_client, invitee, code)

    assert again.status_code == 200
    assert again.json() == first.json()


async def test_an_old_account_rereading_its_link_arrival_gets_it(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2)
    code = await link_code(api_client, owner)
    first = await claim(api_client, arrival, code)
    await age(db_engine, arrival, MONTH)

    again = await claim(api_client, arrival, code)

    assert again.status_code == 200
    assert again.json() == first.json()


# --------------------------------------------------------------------------
# Precedence: the other answers are unchanged for an old account
# --------------------------------------------------------------------------


async def test_an_unknown_code_is_still_a_404_for_an_old_account(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (old,) = await users(db_engine, 1)
    await age(db_engine, old, YEARS)

    assert (await claim(api_client, old, "no-such-code")).status_code == 404


async def test_an_old_account_s_own_codes_are_still_the_own_invite_409(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (old,) = await users(db_engine, 1)
    code = await single_use_code(api_client, old)
    link = await link_code(api_client, old)
    await age(db_engine, old, YEARS)

    for own in (code, link):
        response = await claim(api_client, old, own)
        assert response.status_code == 409
        assert response.json()["type"] == "about:blank"


async def test_somebody_else_s_claimed_invite_is_still_the_plain_409(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, first, old = await users(db_engine, 3)
    code = await single_use_code(api_client, referrer)
    await claim(api_client, first, code)
    await age(db_engine, old, YEARS)

    response = await claim(api_client, old, code)

    assert response.status_code == 409
    assert response.json()["type"] == "about:blank"


async def test_an_old_account_holding_another_invite_still_gets_the_plain_409(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, owner, invitee = await users(db_engine, 3)
    held = await single_use_code(api_client, referrer)
    other = await single_use_code(api_client, owner)
    link = await link_code(api_client, owner)
    assert (await claim(api_client, invitee, held)).status_code == 200
    await age(db_engine, invitee, MONTH)

    for code in (other, link):
        response = await claim(api_client, invitee, code)
        assert response.status_code == 409
        assert response.json()["type"] == "about:blank"


async def test_a_new_account_is_unaffected_by_the_rule(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The positive case at age zero, the state every test seeds."""
    referrer, invitee = await users(db_engine, 2)
    response = await claim(api_client, invitee, await single_use_code(api_client, referrer))
    assert response.status_code == 200


# --------------------------------------------------------------------------
# The exact boundary, at the writer: one transaction, so one now()
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "allowed"), [(timedelta(0), True), (timedelta(microseconds=1), False)]
)
async def test_exactly_the_window_is_still_new_and_a_microsecond_more_is_not(
    db_engine: AsyncEngine,
    api_client: httpx.AsyncClient,
    over: timedelta,
    allowed: bool,
) -> None:
    """``now()`` is fixed for a transaction, so ageing the account and claiming
    in one compares against the same instant (decision 264: exactly
    ``CLAIM_WINDOW`` old is new)."""
    referrer, invitee = await users(db_engine, 2)
    code = await single_use_code(api_client, referrer)
    invitee_id = await user_id_of(db_engine, invitee)
    ladder = credit_ladder(Settings(_env_file=None))  # type: ignore[call-arg]

    async with async_sessionmaker(db_engine)() as session:
        await session.execute(AGE, {"age": CLAIM_WINDOW + over, "u": invitee_id})
        if allowed:
            assert (await claim_referral(session, invitee_id, code, ladder=ladder)).code == code
        else:
            with pytest.raises(InviteForNewAccountsError):
                await claim_referral(session, invitee_id, code, ladder=ladder)
        await session.rollback()

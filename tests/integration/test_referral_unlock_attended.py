"""The invite unlock, and the level top-ups it pays (decisions 295 and 308).

Owner, 2026-10-25, "stop gaming": the unlock is written once both hold, in
either order — a qualified invite, and an attended session as a mentee.
Attended, not completed: a session the mentor missed still counts for the mentee
who came.

Owner, 2026-11-05 (decision 308): the unlock is level 3 and an invite alone is
level 2. Whichever event raises a level tops the month up to it, paying only
what the month has not already paid, so a referrer who onboarded (1) and whose
friend then joined (2) and who then attended (3) holds 1, 2, then 3.

Each order is driven through the real writers: the referrer's own onboarding
and the invite's qualification through the API, the attendance through the
settlement sweep (or a session already attended before the invite qualified).
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
from tests.integration.factories import add_session, make_public_mentor, until_blocked
from tests.integration.test_api_attendance import settle
from tests.integration.test_api_referrals import (
    CLAIM,
    COMPLETE,
    balance_of,
    invite,
    seed,
    unlocks_of,
)

from app.domain.credits import LevelFacts, level_allowance
from app.infra.db.referral_writer import qualify_invitee, unlock_referrers
from conftest import api_token, bearer, default_ladder

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LADDER = default_ladder()
#: Level 1, 2 and 3 on the default ladder, from the rule rather than literals.
ONBOARDED = level_allowance(LevelFacts(onboarded=True, invited=False, unlocked=False), LADDER)
INVITED = level_allowance(LevelFacts(onboarded=True, invited=True, unlocked=False), LADDER)
UNLOCKED = level_allowance(LevelFacts(onboarded=True, invited=True, unlocked=True), LADDER)


async def an_onboarded_referrer(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> tuple[UUID, UUID]:
    """A mentee who finished onboarding through the API: level 1, paid."""
    referrer = uuid4()
    referrer_id = await seed(engine, referrer)
    assert (await client.post(COMPLETE, headers=bearer(api_token(referrer)))).status_code == 201
    assert await balance_of(engine, referrer_id) == ONBOARDED
    return referrer, referrer_id


async def qualify_an_invitee(
    client: httpx.AsyncClient, engine: AsyncEngine, referrer: UUID
) -> None:
    """An invitee of ``referrer`` claims and finishes onboarding."""
    invitee = uuid4()
    await seed(engine, invitee)
    code = (await invite(client, referrer, f"{invitee}@example.com")).json()["code"]
    token = bearer(api_token(invitee))
    assert (await client.post(CLAIM, json={"code": code}, headers=token)).status_code < 300
    assert (await client.post(COMPLETE, headers=token)).status_code < 300


async def a_session_attended_by(
    engine: AsyncEngine, mentee: UUID, *, status: str, mentor_came: bool, mentee_came: bool = True
) -> UUID:
    mentor = await make_public_mentor(engine, f"unlock-{uuid4().hex[:8]}")
    return await add_session(
        engine,
        mentor,
        mentee=mentee,
        status=status,
        attendance="attended" if mentor_came else "pending",
        mentee_attendance="attended" if mentee_came else "pending",
    )


async def unlocked(client: httpx.AsyncClient, auth_id: UUID) -> Any:
    body = (await client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()
    return body["credits"]["monthly"]["unlocked"]


async def test_qualified_first_then_attended_unlocks_at_the_settlement(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Level 2 at once; the unlock and level 3 wait for the session. The mentor
    never came, and the mentee who did still attended."""
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    await qualify_an_invitee(api_client, db_engine, referrer)

    assert await balance_of(db_engine, referrer_id) == INVITED
    assert await unlocks_of(db_engine, referrer_id) == 0
    assert await unlocked(api_client, referrer) is False

    await a_session_attended_by(db_engine, referrer_id, status="confirmed", mentor_came=False)
    assert await settle(db_engine) >= 1

    assert await unlocks_of(db_engine, referrer_id) == 1
    assert await unlocked(api_client, referrer) is True
    assert await balance_of(db_engine, referrer_id) == UNLOCKED


async def test_attended_first_then_qualified_unlocks_at_the_qualification(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    await a_session_attended_by(db_engine, referrer_id, status="completed", mentor_came=True)

    await qualify_an_invitee(api_client, db_engine, referrer)

    assert await unlocks_of(db_engine, referrer_id) == 1
    assert await balance_of(db_engine, referrer_id) == UNLOCKED


async def test_a_session_the_mentee_missed_unlocks_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    await qualify_an_invitee(api_client, db_engine, referrer)

    await a_session_attended_by(
        db_engine, referrer_id, status="confirmed", mentor_came=True, mentee_came=False
    )
    await settle(db_engine)

    assert await unlocks_of(db_engine, referrer_id) == 0
    assert await balance_of(db_engine, referrer_id) == INVITED


async def test_a_missed_session_then_a_qualified_invite_unlocks_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Through the qualification, which reads attendance itself: a session the
    mentee was expected at and missed (`no_show`) is not attended."""
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    await a_session_attended_by(
        db_engine, referrer_id, status="no_show", mentor_came=True, mentee_came=False
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_participants SET attendance_status = 'no_show' WHERE user_id = :u"
            ),
            {"u": referrer_id},
        )

    await qualify_an_invitee(api_client, db_engine, referrer)

    assert await unlocks_of(db_engine, referrer_id) == 0
    assert await balance_of(db_engine, referrer_id) == INVITED


async def test_attending_without_a_qualified_invite_unlocks_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Attending alone moves no level, so the settlement pays nothing more."""
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    # An invite sent and claimed, but the invitee never finishes.
    invitee = uuid4()
    await seed(db_engine, invitee, complete=False)
    code = (await invite(api_client, referrer, "late@example.com")).json()["code"]
    await api_client.post(CLAIM, json={"code": code}, headers=bearer(api_token(invitee)))

    await a_session_attended_by(db_engine, referrer_id, status="confirmed", mentor_came=True)
    await settle(db_engine)

    assert await unlocks_of(db_engine, referrer_id) == 0
    assert await balance_of(db_engine, referrer_id) == ONBOARDED


async def test_an_invite_before_the_referrer_onboards_pays_at_their_onboarding(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Level 2 is level 1 plus an invite: a referrer who never finished their
    own profile is at 0 when the friend joins, and at 2 the moment they finish."""
    referrer = uuid4()
    referrer_id = await seed(db_engine, referrer)
    await qualify_an_invitee(api_client, db_engine, referrer)
    assert await balance_of(db_engine, referrer_id) == 0

    assert (await api_client.post(COMPLETE, headers=bearer(api_token(referrer)))).status_code == 201

    assert await balance_of(db_engine, referrer_id) == INVITED


async def test_a_second_invitee_pays_nothing_more_and_unlocks_once(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    await qualify_an_invitee(api_client, db_engine, referrer)
    await qualify_an_invitee(api_client, db_engine, referrer)
    assert await balance_of(db_engine, referrer_id) == INVITED

    await a_session_attended_by(db_engine, referrer_id, status="confirmed", mentor_came=True)
    await settle(db_engine)
    await qualify_an_invitee(api_client, db_engine, referrer)

    assert await unlocks_of(db_engine, referrer_id) == 1
    assert await balance_of(db_engine, referrer_id) == UNLOCKED


async def test_a_rise_after_spending_pays_the_difference_not_a_refill(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The allowance rule (owner, 2026-11-05).** A referrer who spent their
    level-1 credit and then reaches level 2 gets one more, not two: the month
    pays the level, and a refill would let spending before each rise collect
    more than it."""
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE credit_lots SET quantity_remaining = 0 WHERE user_id = :u"),
            {"u": referrer_id},
        )

    await qualify_an_invitee(api_client, db_engine, referrer)

    assert await balance_of(db_engine, referrer_id) == INVITED - ONBOARDED


async def test_the_unlock_credits_the_earliest_qualified_invite(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    await qualify_an_invitee(api_client, db_engine, referrer)
    await qualify_an_invitee(api_client, db_engine, referrer)

    await a_session_attended_by(db_engine, referrer_id, status="confirmed", mentor_came=True)
    await settle(db_engine)

    async with db_engine.connect() as conn:
        credited, earliest = (
            await conn.execute(
                text(
                    "SELECT u.unlocked_by_referral_id, "
                    "(SELECT id FROM referrals WHERE referrer_id = :u "
                    " ORDER BY qualified_at, id LIMIT 1) "
                    "FROM referral_unlocks u WHERE u.user_id = :u"
                ),
                {"u": referrer_id},
            )
        ).one()
    assert credited == earliest


async def test_a_grandfathered_user_is_at_level_three_and_paid_it_once(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Migrated users were unlocked with no invite and no onboarding the API
    wrote. The unlock alone is level 3; an invite and a session of theirs
    top their month up to it once, and the unlock is never taken away."""
    referrer = uuid4()
    referrer_id = await seed(db_engine, referrer)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id) VALUES (:u, NULL)"
            ),
            {"u": referrer_id},
        )

    await qualify_an_invitee(api_client, db_engine, referrer)
    await a_session_attended_by(db_engine, referrer_id, status="confirmed", mentor_came=True)
    await settle(db_engine)

    assert await balance_of(db_engine, referrer_id) == UNLOCKED
    assert await unlocks_of(db_engine, referrer_id) == 1
    assert await unlocked(api_client, referrer) is True


async def test_the_stats_count_attended_sessions_for_the_invite_card(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """`sessions_attended` counts the session the mentor missed, which
    `sessions_held` (completed only) does not."""
    mentee = uuid4()
    mentee_id = await seed(db_engine, mentee)
    await a_session_attended_by(db_engine, mentee_id, status="no_show", mentor_came=False)
    await a_session_attended_by(
        db_engine, mentee_id, status="no_show", mentor_came=True, mentee_came=False
    )

    body = (
        await api_client.get("/api/v1/me/mentee-stats", headers=bearer(api_token(mentee)))
    ).json()

    assert body["sessions_attended"] == 1
    assert body["sessions_held"] == 0


async def a_claimed_invitee(client: httpx.AsyncClient, engine: AsyncEngine, referrer: UUID) -> UUID:
    """An invitee of ``referrer`` who has claimed but not yet qualified."""
    invitee = uuid4()
    invitee_id = await seed(engine, invitee)
    code = (await invite(client, referrer, f"{invitee}@example.com")).json()["code"]
    await client.post(CLAIM, json={"code": code}, headers=bearer(api_token(invitee)))
    return invitee_id


async def attend_and_unlock(engine: AsyncEngine, referrer_id: UUID) -> None:
    """The settling half, in its own transaction: the referrer's attendance
    becomes `ATTENDED` (as `observe_presence` does inside the settlement run)
    and `unlock_referrers` is asked, before anything commits."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as settling:
        await settling.execute(
            text(
                "UPDATE session_participants SET attendance_status = 'attended' WHERE user_id = :u"
            ),
            {"u": referrer_id},
        )
        await unlock_referrers(settling, [referrer_id], ladder=LADDER, now=dt.datetime.now(dt.UTC))
        await settling.commit()


async def test_a_qualification_and_an_attendance_racing_still_unlock_and_pay_once(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The write skew the referrer's row lock exists for.** The qualification
    looks for attendance and finds none (it is not committed); the settling
    transaction, unlocked, would not see the uncommitted qualification, and
    neither would write. Locked, the settling side waits, then sees it — and
    tops the month up to level 3 once, not on top of the qualification's 2."""
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    invitee_id = await a_claimed_invitee(api_client, db_engine, referrer)
    await a_session_attended_by(
        db_engine, referrer_id, status="confirmed", mentor_came=True, mentee_came=False
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as qualifying:
        await qualify_invitee(qualifying, invitee_id, ladder=LADDER)
        settling = asyncio.create_task(attend_and_unlock(db_engine, referrer_id))
        await until_blocked(db_engine)
        await qualifying.commit()
    await settling

    assert await unlocks_of(db_engine, referrer_id) == 1
    assert await balance_of(db_engine, referrer_id) == UNLOCKED


async def test_two_invitees_qualifying_at_once_pay_one_top_up(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Both would read "nothing paid for level 2" before either commits unless
    the second waits on the referrer's lock; then it sees the first's top-up
    and owes nothing."""
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    first = await a_claimed_invitee(api_client, db_engine, referrer)
    second = await a_claimed_invitee(api_client, db_engine, referrer)

    async def qualify(invitee_id: UUID) -> None:
        async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
            await qualify_invitee(session, invitee_id, ladder=LADDER)
            await session.commit()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as qualifying:
        await qualify_invitee(qualifying, first, ladder=LADDER)
        racing = asyncio.create_task(qualify(second))
        await until_blocked(db_engine)
        await qualifying.commit()
    await racing

    assert await balance_of(db_engine, referrer_id) == INVITED

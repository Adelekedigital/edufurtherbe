"""The one-off top-up when the level rule changes mid-month (decision 370):
a mentee who completed a session before the rule paid level 2 is owed the
difference now, paid once and silently, and a re-run pays nothing.

The database is shared with other suites, so each test asserts about the
mentee it made rather than the whole list.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import httpx
import pytest
from scripts.top_up_levels import run
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_referrals import balance_of
from tests.integration.test_referral_unlock_attended import (
    ATTENDED,
    ONBOARDED,
    a_session_attended_by,
    an_onboarded_referrer,
)

from app.core.config import Settings
from app.infra.db.credit_grants import owed_top_ups
from conftest import default_ladder

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LADDER = default_ladder()
#: The settings the default ladder is built from, never the process environment.
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


async def owed(engine: AsyncEngine) -> dict[UUID, int]:
    async with async_sessionmaker(engine)() as session:
        return await owed_top_ups(session, now=dt.datetime.now(dt.UTC), ladder=LADDER)


async def a_mentee_who_attended_before_the_rule(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> UUID:
    """Onboarded and paid level 1; a completed session recorded with no
    settlement since, as every mentee who completed one before decision 370 is."""
    _, mentee_id = await an_onboarded_referrer(client, engine)
    await a_session_attended_by(engine, mentee_id, status="completed", mentor_came=True)
    assert await balance_of(engine, mentee_id) == ONBOARDED
    return mentee_id


async def emails_to(engine: AsyncEngine, user_id: UUID) -> int:
    """Messages to the person: every outbox row but analytics, which records
    each grant (`credit_granted`) and is not something anybody is told."""
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE entity_id = :u AND destination <> 'analytics'"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
        )


async def test_a_session_the_mentor_missed_is_not_a_level(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Completed, not attended (decision 370): the mentee who came to a session
    the mentor missed is refunded, and does not also rise a level for good."""
    _, mentee_id = await an_onboarded_referrer(api_client, db_engine)
    await a_session_attended_by(db_engine, mentee_id, status="no_show", mentor_came=False)

    assert mentee_id not in await owed(db_engine)


async def test_a_user_with_no_mentee_goal_is_owed_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Credits buy sessions as a mentee: no goal is level 0, completed session
    or not."""
    mentee_id = await a_mentee_who_attended_before_the_rule(api_client, db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM mentee_goals WHERE user_id = :u"), {"u": mentee_id})

    assert mentee_id not in await owed(db_engine)


async def test_the_top_up_is_silent(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> None:
    """As a settlement's top-up is: no outbox row for anybody it pays."""
    mentee_id = await a_mentee_who_attended_before_the_rule(api_client, db_engine)
    before = await emails_to(db_engine, mentee_id)

    await run(apply=True, engine=db_engine, settings=SETTINGS, now=dt.datetime.now(dt.UTC))

    assert await balance_of(db_engine, mentee_id) == ATTENDED
    assert await emails_to(db_engine, mentee_id) == before


async def test_a_mentee_who_attended_is_owed_the_difference(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    mentee_id = await a_mentee_who_attended_before_the_rule(api_client, db_engine)

    assert (await owed(db_engine)).get(mentee_id) == ATTENDED - ONBOARDED


async def test_a_mentee_at_their_level_is_owed_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    _, mentee_id = await an_onboarded_referrer(api_client, db_engine)

    assert mentee_id not in await owed(db_engine)


async def test_a_dry_run_pays_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    mentee_id = await a_mentee_who_attended_before_the_rule(api_client, db_engine)

    code = await run(apply=False, engine=db_engine, settings=SETTINGS, now=dt.datetime.now(dt.UTC))

    assert code == 0
    assert await balance_of(db_engine, mentee_id) == ONBOARDED


async def test_apply_pays_once_and_a_rerun_pays_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    mentee_id = await a_mentee_who_attended_before_the_rule(api_client, db_engine)
    now = dt.datetime.now(dt.UTC)

    await run(apply=True, engine=db_engine, settings=SETTINGS, now=now)
    assert await balance_of(db_engine, mentee_id) == ATTENDED

    await run(apply=True, engine=db_engine, settings=SETTINGS, now=now)
    assert await balance_of(db_engine, mentee_id) == ATTENDED
    assert mentee_id not in await owed(db_engine)

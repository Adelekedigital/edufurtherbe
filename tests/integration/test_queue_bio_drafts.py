"""The bio draft backfill (owner, 2026-11-25): mentees who finished before
drafts existed are queued to the onboarding draft's own callback, and only
those whose bio is still unwritten.

The database is shared with other suites, so each test asserts about the
people it made rather than the whole list.
"""

from __future__ import annotations

import datetime as dt

import pytest
from scripts.queue_bio_drafts import run
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.scheduler_fakes import FakeScheduler
from tests.integration.test_api_mentee_bio_draft import (
    DRAFT,
    PATH,
    Person,
    World,
    a_mentee,
    bio_of,
    deliver,
    run_sql,
    world,  # noqa: F401  (the fixture the end-to-end test uses)
)

from app.infra.db.bio_draft_store import owed_bio_drafts

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CALLBACK = f"https://api.example.test{PATH}"
NOW = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.UTC)


async def finished(engine: AsyncEngine, **kwargs: object) -> Person:
    """A mentee who finished onboarding (in the legacy app, or before drafts)."""
    person = await a_mentee(engine, **kwargs)  # type: ignore[arg-type]
    await run_sql(
        engine,
        "INSERT INTO user_onboarding (user_id, completed_at) VALUES (:u, now())",
        u=person.id,
    )
    return person


async def owed(engine: AsyncEngine) -> set[str]:
    async with async_sessionmaker(engine)() as session:
        return {str(user_id) for user_id in await owed_bio_drafts(session)}


async def test_a_finished_mentee_with_an_empty_or_blank_bio_is_owed(db_engine: AsyncEngine) -> None:
    empty = await finished(db_engine)
    blank = await finished(db_engine, about_me="   ")
    no_profile = await finished(db_engine)
    await run_sql(db_engine, "DELETE FROM user_profiles WHERE user_id = :u", u=no_profile.id)

    assert {str(empty.id), str(blank.id), str(no_profile.id)} <= await owed(db_engine)


async def test_anyone_not_owed_a_draft_is_left_out(db_engine: AsyncEngine) -> None:
    own_bio = await finished(db_engine, about_me="I wrote this myself.")
    drafted = await finished(db_engine)
    await run_sql(
        db_engine,
        "UPDATE user_profiles SET about_me = 'A draft.', about_me_drafted_at = now(), "
        "about_me_prompt_version = 'bio_draft-v1' WHERE user_id = :u",
        u=drafted.id,
    )
    unfinished = await a_mentee(db_engine)
    mentor = await finished(db_engine, mentor=True)
    no_goal = await finished(db_engine, goal=False)
    deleted = await finished(db_engine)
    await run_sql(db_engine, "UPDATE users SET deleted_at = now() WHERE id = :u", u=deleted.id)

    left_out = {str(p.id) for p in (own_bio, drafted, unfinished, mentor, no_goal, deleted)}
    assert not left_out & await owed(db_engine)


async def test_a_dry_run_queues_nothing(db_engine: AsyncEngine) -> None:
    await finished(db_engine)
    scheduler = FakeScheduler()

    code = await run(
        apply=False, engine=db_engine, scheduler=scheduler, callback_url=CALLBACK, now=NOW
    )

    assert code == 0
    assert scheduler.published == []


async def test_apply_queues_each_to_the_onboarding_callback_spaced_apart(
    db_engine: AsyncEngine,
) -> None:
    person = await finished(db_engine)
    scheduler = FakeScheduler()

    code = await run(
        apply=True,
        engine=db_engine,
        scheduler=scheduler,
        callback_url=CALLBACK,
        now=NOW,
        spacing=dt.timedelta(seconds=10),
    )

    assert code == 0
    assert {"user_id": str(person.id)} in [m["body"] for m in scheduler.published]
    assert {m["url"] for m in scheduler.published} == {CALLBACK}
    times = [m["at"] for m in scheduler.published]
    assert times == [NOW + i * dt.timedelta(seconds=10) for i in range(len(times))]


async def test_a_failed_publish_is_reported_and_the_run_exits_nonzero(
    db_engine: AsyncEngine,
) -> None:
    await finished(db_engine)

    code = await run(
        apply=True,
        engine=db_engine,
        scheduler=FakeScheduler(fails=True),
        callback_url=CALLBACK,
        now=NOW,
    )

    assert code == 1


async def test_a_queued_delivery_writes_a_draft_and_a_rerun_skips_them(world: World) -> None:  # noqa: F811
    """End to end through the real callback: the draft lands as a draft, and
    the person is no longer owed, so a second run does not queue them."""
    person = await finished(world.engine)
    scheduler = FakeScheduler()
    await run(apply=True, engine=world.engine, scheduler=scheduler, callback_url=CALLBACK, now=NOW)
    assert {"user_id": str(person.id)} in [m["body"] for m in scheduler.published]

    response = await deliver(world, person)

    assert response.status_code == 200
    bio = await bio_of(world.engine, person)
    assert bio["about_me"] == DRAFT
    assert bio["about_me_drafted_at"] is not None
    assert str(person.id) not in await owed(world.engine)

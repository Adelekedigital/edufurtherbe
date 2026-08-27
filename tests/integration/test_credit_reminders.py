"""Nudging people about credits that are about to expire, and not the rest.

**The tests that matter are the exclusions.** A nudge about credits running out
is only worth sending to somebody whose credits are running out; every group this
must skip — the starter holder, the already-expired, the spent-down — would
otherwise be nudged every fortnight forever, which is what teaches people to
filter a sender.

**And the repeat.** `entity_id` here is the user rather than a session, so a
dedup key without the period would be unique across their whole lifetime: nudged
once, ever, and never again.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db.credit_reminders import remind_about_expiring_credits
from app.infra.db.engine import create_session_factory

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: Two weeks out from the boundary, so `c14` is due and `c7` is not.
FOURTEEN_OUT = dt.datetime(2026, 9, 17, 6, 0, tzinfo=dt.UTC)
SEVEN_OUT = dt.datetime(2026, 9, 24, 6, 0, tzinfo=dt.UTC)
BOUNDARY = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


async def a_user(engine: AsyncEngine, *, deleted: bool = False) -> UUID:
    tag = uuid4()
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO users (email, primary_role, timezone, deleted_at) "
                            "VALUES (:e, 'mentee', 'Africa/Lagos', :d) RETURNING id"
                        ),
                        {
                            "e": f"{tag}@example.com",
                            "d": dt.datetime(2026, 1, 1, tzinfo=dt.UTC) if deleted else None,
                        },
                    )
                ).scalar_one()
            )
        )


async def a_lot(
    engine: AsyncEngine,
    user_id: UUID,
    *,
    remaining: int = 3,
    expires: dt.datetime | None = BOUNDARY,
    source: str = "monthly_free",
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO credit_lots "
                "(user_id, source, quantity_granted, quantity_remaining, expires_at) "
                "VALUES (:u, :s, 3, :r, :e)"
            ),
            {"u": user_id, "s": source, "r": remaining, "e": expires},
        )


async def queued_for(engine: AsyncEngine, user_id: UUID) -> list[tuple[str, str]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT payload ->> 'kind', payload ->> 'interval' FROM outbox_events "
                "WHERE event_type = 'credits_expiring' "
                "AND payload ->> 'recipient_id' = :u ORDER BY created_at, id"
            ),
            {"u": str(user_id)},
        )
        return [(r[0], r[1]) for r in rows]


async def sweep(engine: AsyncEngine, *, now: dt.datetime) -> int:
    factory = create_session_factory(engine)
    async with factory() as db:
        queued = await remind_about_expiring_credits(db, now=now)
        await db.commit()
        return queued


# --------------------------------------------------------------------------
# Who is nudged
# --------------------------------------------------------------------------


async def test_a_holder_of_expiring_credits_is_nudged_at_two_weeks(
    db_engine: AsyncEngine,
) -> None:
    user = await a_user(db_engine)
    await a_lot(db_engine, user)

    await sweep(db_engine, now=FOURTEEN_OUT)

    assert await queued_for(db_engine, user) == [("c14:2026-10", "two weeks")]


async def test_the_same_holder_is_nudged_again_at_one_week(db_engine: AsyncEngine) -> None:
    """Two offsets, one expiry, two messages — and the second is not refused by
    the index, because the kind differs."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user)
    await sweep(db_engine, now=FOURTEEN_OUT)

    await sweep(db_engine, now=SEVEN_OUT)

    assert await queued_for(db_engine, user) == [
        ("c14:2026-10", "two weeks"),
        ("c7:2026-10", "one week"),
    ]


async def test_the_words_travel_with_the_offset(db_engine: AsyncEngine) -> None:
    """`interval` is carried rather than derived at send time, so a template
    cannot render "two weeks" one week out."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user)

    await sweep(db_engine, now=SEVEN_OUT)

    assert await queued_for(db_engine, user) == [("c7:2026-10", "one week")]


# --------------------------------------------------------------------------
# Who is not — the half that decides whether people mute the sender
# --------------------------------------------------------------------------


async def test_a_starter_holder_is_never_nudged(db_engine: AsyncEngine) -> None:
    """The starter never expires, so nothing of theirs is running out — a filter
    written as "has a balance" would nudge them every fortnight for the life of
    the account.

    **No clause in the query names this case**, and the honest reading is that
    the exclusion is structural: the window is bounded on both sides, so a lot
    with no expiry has no date to fall inside it. An explicit
    `expires_at IS NOT NULL` was written first and then removed — it is
    unconditionally redundant, which two attempted rewrites confirmed rather than
    assumed. Widening the range with `coalesce(expires_at, 'infinity')` still
    excludes the starter, because a sentinel far future is outside any window an
    offset produces.

    So this test guards the *behaviour* and cannot fail for the reason its name
    suggests. It would go red if the window ever became one-sided.
    """
    user = await a_user(db_engine)
    await a_lot(db_engine, user, remaining=1, expires=None, source="profile_completed")

    await sweep(db_engine, now=FOURTEEN_OUT)

    assert await queued_for(db_engine, user) == []


async def test_somebody_with_nothing_left_is_not_nudged(db_engine: AsyncEngine) -> None:
    """Nothing to lose and nothing to do about it. The call to action would be
    "use your credits" addressed to somebody with none."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user, remaining=0)

    await sweep(db_engine, now=FOURTEEN_OUT)

    assert await queued_for(db_engine, user) == []


async def test_an_already_expired_lot_is_not_about_to_expire(db_engine: AsyncEngine) -> None:
    """Past tense, not future. The sweep is `spendable_now`-filtered, so a lot
    the balance already stopped counting cannot produce urgency about itself."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user, expires=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))

    await sweep(db_engine, now=FOURTEEN_OUT)

    assert await queued_for(db_engine, user) == []


async def test_a_deleted_user_is_not_nudged(db_engine: AsyncEngine) -> None:
    user = await a_user(db_engine, deleted=True)
    await a_lot(db_engine, user)

    await sweep(db_engine, now=FOURTEEN_OUT)

    assert await queued_for(db_engine, user) == []


async def test_nobody_is_nudged_outside_the_window(db_engine: AsyncEngine) -> None:
    """Three weeks out is neither offset. The accepting cases above all sit
    inside a window; without this, a sweep matching everything would pass every
    one of them."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user)

    await sweep(db_engine, now=dt.datetime(2026, 9, 10, 6, 0, tzinfo=dt.UTC))

    assert await queued_for(db_engine, user) == []


# --------------------------------------------------------------------------
# Running twice, and running next month
# --------------------------------------------------------------------------


async def test_a_second_sweep_in_the_window_queues_nothing_new(
    db_engine: AsyncEngine,
) -> None:
    """The index refuses it, so the cadence is free: a daily job re-runs safely
    and a missed day costs a nudge rather than duplicating one."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user)
    await sweep(db_engine, now=FOURTEEN_OUT)

    await sweep(db_engine, now=FOURTEEN_OUT + dt.timedelta(hours=6))

    assert await queued_for(db_engine, user) == [("c14:2026-10", "two weeks")]


async def test_next_month_nudges_again(db_engine: AsyncEngine) -> None:
    """**The test the period-in-the-key exists for.**

    Without it, `(entity_id, event_type, kind, recipient)` is unique across the
    user's lifetime and this second nudge is silently dropped — one email, ever,
    for a message meant to arrive every month.
    """
    user = await a_user(db_engine)
    await a_lot(db_engine, user)
    await sweep(db_engine, now=FOURTEEN_OUT)
    await a_lot(db_engine, user, expires=dt.datetime(2026, 11, 1, tzinfo=dt.UTC))

    await sweep(db_engine, now=dt.datetime(2026, 10, 17, 6, 0, tzinfo=dt.UTC))

    assert await queued_for(db_engine, user) == [
        ("c14:2026-10", "two weeks"),
        ("c14:2026-11", "two weeks"),
    ]


async def test_one_nudge_for_several_lots_sharing_an_expiry(
    db_engine: AsyncEngine,
) -> None:
    """Three monthly lots with one deadline is one person with one deadline. The
    `GROUP BY` is what makes that true; without it they get three identical
    emails and the index cannot tell them apart, because the kind is the same."""
    user = await a_user(db_engine)
    await a_lot(db_engine, user, remaining=1)
    await a_lot(db_engine, user, remaining=2, source="referral_unlock")

    queued = await sweep(db_engine, now=FOURTEEN_OUT)

    assert queued == 1
    assert await queued_for(db_engine, user) == [("c14:2026-10", "two weeks")]


async def test_a_naive_now_is_refused(db_engine: AsyncEngine) -> None:
    """The same refusal `end_of_month` and the expiry sweep make. A naive
    datetime resolves against the host's zone and moves the whole window."""
    factory = create_session_factory(db_engine)
    async with factory() as db:
        with pytest.raises(ValueError, match="aware datetime"):
            await remind_about_expiring_credits(db, now=dt.datetime(2026, 9, 17, 6, 0))

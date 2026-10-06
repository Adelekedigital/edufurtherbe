"""Who is waiting for a feature that has not shipped: record it, read it, withdraw it.

**Not mentor-scoped.** Explore's no-mentors state is a signed-in mentee's, so
this sits with the rest of `/me` for any account (#365). `user_id` is in the
`WHERE` on every path, read and write, rather than checked after a fetch.

**No `notified_at` writer here.** The column exists so a future send cannot tell
one person twice, and nothing sends yet — a setter with no caller is how a
half-built mechanism gets mistaken for a working one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.interest import MAX_FEATURES_PER_ACCOUNT
from app.infra.db.models.platform import FeatureInterest

__all__ = ["own_interests", "register_interest", "withdraw_interest"]


class TooManyInterestsError(ConflictError):
    """This account already waits for as many features as it may.

    **A bound on rows, not a product rule**, so a `409` rather than a `422`: the
    key was well formed and the request was legal — there is simply no room.
    Nobody reaches it by using the product, which is why the message says what
    to do rather than apologising.

    **Approximate under concurrency, and deliberately left so.** The count runs
    inside the caller's transaction at `READ COMMITTED`, so parallel requests
    with distinct keys can each see a count below the cap and each commit — the
    achievable total is the cap plus one burst's parallelism, never unbounded,
    because every insert after that counts past it and rolls back. Making it
    exact would need a lock on a row nobody is reading, to hold a number that
    exists only to stop a loop. Said here because the alternative is a comment
    that reads as a guarantee.
    """


async def register_interest(session: AsyncSession, user_id: UUID, feature: str) -> None:
    """Record that this account is waiting for `feature`. No commit.

    **Pressing twice is pressing once**, and that is the uniqueness constraint
    doing it rather than a read-then-write: `ON CONFLICT DO NOTHING` means two
    simultaneous presses cannot both insert, and no `Idempotency-Key` is needed
    on a write whose repeat is free.

    **The cap is checked after the insert, deliberately.** Checking first would
    refuse a *repeat* press from an account already at the cap — a button that
    worked yesterday and errors today, for somebody who asked for nothing new.
    Inserting first makes the repeat a no-op that returns early, and the count
    then includes the row just added, so the comparison needs no "+1" to get
    right. Raising here rolls the insert back with the request.
    """
    inserted = (
        await session.execute(
            insert(FeatureInterest)
            .values(user_id=user_id, feature=feature)
            .on_conflict_do_nothing(index_elements=["user_id", "feature"])
            .returning(FeatureInterest.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        # Already registered. The caller asked for a state that already holds.
        return

    waiting = (
        await session.execute(select(func.count()).where(FeatureInterest.user_id == user_id))
    ).scalar_one()
    if waiting > MAX_FEATURES_PER_ACCOUNT:
        raise TooManyInterestsError(
            f"you are already waiting for {MAX_FEATURES_PER_ACCOUNT} features; "
            "withdraw one before adding another"
        )


async def own_interests(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """What this account is waiting for, oldest first.

    **Whole, with no cursor.** The per-account cap bounds this absolutely, so
    the answer fits in one response and `next_cursor` is always null — the
    envelope is there because ADR 0016 puts it on every list, not because there
    is a second page to fetch.

    `notified_at` is not returned. It describes what the platform has done, not
    what the person asked for, and a client showing "we told you" for a message
    nobody can yet send would be describing a mechanism that does not exist.
    """
    rows = (
        (
            await session.execute(
                select(FeatureInterest.feature, FeatureInterest.created_at)
                .where(FeatureInterest.user_id == user_id)
                # Oldest first, so the order is the order they asked in — stable
                # across reads, which `created_at` alone is not: two rows written
                # in one transaction share it, and `id` is v7 so it breaks the
                # tie the same way.
                .order_by(FeatureInterest.created_at, FeatureInterest.id)
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def withdraw_interest(session: AsyncSession, user_id: UUID, feature: str) -> bool:
    """Stop waiting for `feature`. ``False`` if this account was not. No commit.

    **Deleted rather than marked**, because withdrawing is the absence of the
    row: a status column would enumerate three states (`waiting`, `withdrawn`,
    `notified`) where two facts already answer them, and a withdrawn row would
    have to be excluded from every read for ever. Pressing the button again
    simply registers afresh.

    Returns whether a row went, which the route turns into `404` — the same rule
    calendar disconnect follows: somebody who believes they turned something off
    needs to know if they did not.
    """
    gone = (
        await session.execute(
            delete(FeatureInterest)
            .where(
                FeatureInterest.user_id == user_id,
                FeatureInterest.feature == feature,
            )
            .returning(FeatureInterest.id)
        )
    ).scalar_one_or_none()
    return gone is not None

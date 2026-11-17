"""Who is waiting for a feature that has not shipped: record it, read it, withdraw it.

**Not mentor-scoped.** Explore's no-mentors state is a signed-in mentee's, so
this sits with the rest of `/me` for any account (#365). `user_id` is in the
`WHERE` on every path, read and write, rather than checked after a fetch.

**No `notified_at` writer here.** The column exists so a future send cannot tell
one person twice, and nothing sends yet — a setter with no caller is how a
half-built mechanism gets mistaken for a working one. What a send reads is
here: `waiting_for`, the one recipient read, which drops closed accounts.
"""

from __future__ import annotations

from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import bindparam, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.interest import MAX_FEATURES_PER_ACCOUNT
from app.infra.db.models.platform import FeatureInterest
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

__all__ = [
    "USER_STATEMENTS",
    "Waiting",
    "own_interests",
    "register_interest",
    "waiting_for",
    "withdraw_interest",
]


class Waiting(NamedTuple):
    """One person still waiting: the row to stamp `notified_at` on, and who."""

    interest_id: UUID
    user_id: UUID


#: Live accounts not yet told about one feature, oldest first (#374). Locks the
#: rows it returns and skips rows another send holds, so two runs of one send
#: cannot pick the same person.
WAITING = (
    select(FeatureInterest.id, FeatureInterest.user_id)
    .join(User, User.id == FeatureInterest.user_id)
    .where(
        FeatureInterest.feature == bindparam("feature"),
        FeatureInterest.notified_at.is_(None),
        LIVE,
    )
    .order_by(FeatureInterest.created_at, FeatureInterest.id)
    .with_for_update(of=FeatureInterest, skip_locked=True)
)

#: Statements reading an existing `users` row, for the `LIVE` parity test.
USER_STATEMENTS = (WAITING,)


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


async def waiting_for(session: AsyncSession, feature: str, *, limit: int) -> list[Waiting]:
    """Who may be told `feature` has shipped: **the one recipient read** (#374).

    A row is not a claim its account can still be contacted. It was written
    while the account was live, and a soft delete leaves it in place, so this
    joins `users` and composes `LIVE` in the same statement rather than
    filtering a fetched list. The rows themselves stay: purge removes them with
    the account, and a restored account keeps what it asked for.

    Not yet told, oldest first, at most `limit`. A send stamps `notified_at`
    on the returned `interest_id`s (never by user and feature, which a
    withdraw-and-register would move to a new row) in the same transaction that
    enqueues the messages, commits, and asks again; the row lock makes a second
    concurrent run skip this batch rather than send it twice.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    rows = await session.execute(WAITING.limit(limit), {"feature": feature})
    return [Waiting(interest_id=row.id, user_id=row.user_id) for row in rows]

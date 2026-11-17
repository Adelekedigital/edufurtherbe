"""Each UTC day's random key for profile link visitor keys (decision 348).

A visitor key is an HMAC under the day's key (`domain.link_tracking`). The key
is 32 random bytes, drawn by whichever process asks first that day, and is
deleted once its day is over: after that, no stored ``visitor_key`` of that day
can be recomputed from a person, so it cannot be linked to anybody.
"""

from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import Date, Exists, LargeBinary, Select, delete, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.link_tracking import DAY_KEY_BYTES
from app.infra.db.models.platform import VisitorDayKey


def _later_than(day: dt.date) -> Exists:
    """Whether some process has already moved on to a later day."""
    return exists().where(VisitorDayKey.day > day)


def _key_of(day: dt.date) -> Select[tuple[bytes]]:
    """``day``'s key, unless a later day's exists: then ``day`` is over."""
    return select(VisitorDayKey.key).where(VisitorDayKey.day == day, ~_later_than(day))


async def day_key(session: AsyncSession, day: dt.date) -> bytes | None:
    """The key for ``day``, created if no process has yet; ``None`` once a
    later day's key exists, so nothing is recorded under an ended day.

    Read first; on a miss, ``INSERT ... ON CONFLICT (day) DO NOTHING`` (only
    while no later day exists), delete every earlier day, read again and
    commit, so two processes asking at once store one key and both return it.
    Committed on its own, not with the caller's write, so a request that then
    stores nothing still leaves the key every other process will read. A
    request that computed its day just before midnight, after another process
    started the next day, gets ``None`` rather than a deleted key back.
    """
    key = await session.scalar(_key_of(day))
    if key is None:
        drawn = select(
            literal(day, Date), literal(secrets.token_bytes(DAY_KEY_BYTES), LargeBinary)
        ).where(~_later_than(day))
        await session.execute(
            insert(VisitorDayKey)
            .from_select(["day", "key"], drawn)
            .on_conflict_do_nothing(index_elements=["day"])
        )
        await prune_past_keys(session, today=day, dry_run=False)
        key = await session.scalar(_key_of(day))
        await session.commit()
    return None if key is None else bytes(key)


async def prune_past_keys(session: AsyncSession, *, today: dt.date, dry_run: bool) -> int:
    """Delete every key for a day before ``today``; the count. A dry run counts.

    The caller commits: the daily retention job does, and `day_key` does.
    """
    past = VisitorDayKey.day < today
    if dry_run:
        return int(await session.scalar(select(func.count()).where(past)) or 0)
    deleted = await session.execute(delete(VisitorDayKey).where(past).returning(VisitorDayKey.id))
    return len(deleted.all())

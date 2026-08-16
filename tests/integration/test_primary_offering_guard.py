"""The trigger protecting a mentor's primary offering.

**The first business-rule trigger in this schema**, and it exists because a
foreign key cannot do the job. `mentor_profiles.primary_session_type_id` is
declared `ON DELETE RESTRICT`, but nothing ever hard-deletes a session type:
retirement is `deleted_at` or `is_active = false`, and both are UPDATEs. A
foreign key never sees an UPDATE, so `RESTRICT` would sit there looking like a
guard and catching nothing — which is precisely what D90 records.

Both paths are tested separately rather than parametrised over one, because they
are two different clauses in the trigger and a test covering one would pass with
the other deleted.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def make_mentor_with_offering(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    """A mentor, one session type, and the pointer set — the state to protect."""
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Ada', 'mentor', 'UTC') RETURNING id"
                ),
                {"e": f"guard-{tag}-{uuid4()}@example.test"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO mentor_profiles (user_id, approval_status, listing_status) "
                "VALUES (:u, 'approved', 'listed')"
            ),
            {"u": user_id},
        )
        session_type_id = (
            await conn.execute(
                text(
                    "INSERT INTO session_types (mentor_user_id, name) "
                    "VALUES (:u, 'General') RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE mentor_profiles SET primary_session_type_id = :s WHERE user_id = :u"),
            {"s": session_type_id, "u": user_id},
        )
    return user_id, session_type_id


async def test_a_primary_offering_cannot_be_soft_deleted(db_engine: AsyncEngine) -> None:
    """The path `ON DELETE RESTRICT` cannot see.

    Nothing hard-deletes a session type, so without the trigger this UPDATE
    succeeds and the mentor's primary points at a deleted row — and every
    fallback that reads through it silently resolves to nothing.
    """
    _, session_type_id = await make_mentor_with_offering(db_engine, "softdelete")

    with pytest.raises(DBAPIError, match="primary offering"):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE session_types SET deleted_at = now() WHERE id = :s"),
                {"s": session_type_id},
            )


async def test_a_primary_offering_cannot_be_deactivated(db_engine: AsyncEngine) -> None:
    """The second path, and a separate clause in the trigger.

    D90 chose one flag for off/closed/hidden deliberately — "no complication,
    `is_active` should act as both turned off and closed" — which means clearing
    it retires the offering just as surely as deleting it.
    """
    _, session_type_id = await make_mentor_with_offering(db_engine, "deactivate")

    with pytest.raises(DBAPIError, match="primary offering"):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE session_types SET is_active = false WHERE id = :s"),
                {"s": session_type_id},
            )


async def test_the_same_offering_can_be_retired_once_it_is_not_the_primary(
    db_engine: AsyncEngine,
) -> None:
    """The guard must be a guard, not a lock.

    Without this, a trigger that refused *every* retirement would pass both tests
    above and make a primary offering permanent — which is worse than the bug it
    replaces, because there would be no way out at all.
    """
    user_id, session_type_id = await make_mentor_with_offering(db_engine, "released")

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET primary_session_type_id = NULL WHERE user_id = :u"),
            {"u": user_id},
        )
        await conn.execute(
            text("UPDATE session_types SET deleted_at = now() WHERE id = :s"),
            {"s": session_type_id},
        )

    async with db_engine.begin() as conn:
        deleted = (
            await conn.execute(
                text("SELECT deleted_at FROM session_types WHERE id = :s"), {"s": session_type_id}
            )
        ).scalar_one()
    assert deleted is not None


async def test_a_non_primary_offering_is_untouched_by_the_guard(db_engine: AsyncEngine) -> None:
    """A mentor's *other* offerings retire normally.

    The trigger fires on every `session_types` UPDATE, so the cheap mistake is a
    predicate that catches rows it was never meant to.
    """
    user_id, _ = await make_mentor_with_offering(db_engine, "second")

    async with db_engine.begin() as conn:
        other = (
            await conn.execute(
                text(
                    "INSERT INTO session_types (mentor_user_id, name) "
                    "VALUES (:u, 'Another') RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE session_types SET deleted_at = now() WHERE id = :s"), {"s": other}
        )

    async with db_engine.begin() as conn:
        deleted = (
            await conn.execute(
                text("SELECT deleted_at FROM session_types WHERE id = :s"), {"s": other}
            )
        ).scalar_one()
    assert deleted is not None


async def test_an_unrelated_update_still_works(db_engine: AsyncEngine) -> None:
    """Renaming a primary offering is not retiring it.

    A trigger written as "refuse any UPDATE while primary" would pass every test
    above and make the offering uneditable — the guard has to be about the two
    columns that mean retirement, not about the row.
    """
    _, session_type_id = await make_mentor_with_offering(db_engine, "rename")

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET name = 'Renamed' WHERE id = :s"), {"s": session_type_id}
        )

    async with db_engine.begin() as conn:
        name = (
            await conn.execute(
                text("SELECT name FROM session_types WHERE id = :s"), {"s": session_type_id}
            )
        ).scalar_one()
    assert name == "Renamed"

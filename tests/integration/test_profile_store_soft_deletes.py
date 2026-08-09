"""Every soft-deletable read in `profile_store`, checked against one rule.

**This rule has now been missed twice in this repository.** First on `users`,
hand-typed into five statements with the fifth — an `UPDATE` — forgotten, so a
soft-deleted user would have been handed a live Supabase account. Then on
`user_awards`, four statements into a new module, where `list_awards` returned
rows the user had deleted.

`test_predicates.py` fixed the first by walking `provisioning_store` for
statements naming `users`. That does not transfer here: `profile_store` builds
its statements **inside functions**, so a module walk finds nothing to inspect
and would pass while looking at an empty set.

So this checks the behaviour instead, and takes the *list of tables that need
checking* from `Base.metadata` rather than from anything a person maintains. A
new soft-deletable table fails `test_every_soft_deletable_table_is_covered`
until somebody either adds a case or exempts it out loud.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.infra.db.base import Base
from app.infra.db.profile_store import get_mentor_profile, list_awards, list_education

# `asyncio` is applied per test rather than to the module: the coverage check
# below is synchronous, and a module-level mark makes pytest warn about it.
pytestmark = [pytest.mark.db]

#: Derived, never typed out. Whatever declares `deleted_at` is what needs a case.
SOFT_DELETABLE = {table.name for table in Base.metadata.tables.values() if "deleted_at" in table.c}

#: `users` is soft-deleted in `api.deps` rather than here — `get_target_user`
#: refuses before any of these functions is reached, and
#: `test_a_soft_deleted_user_is_invisible_even_to_an_admin` asserts it. Named
#: rather than silently absent, so the exemption is a decision and not a gap.
EXEMPT = {"users"}


async def seed_education(conn: Any, user_id: UUID) -> None:
    await conn.execute(
        text(
            "INSERT INTO education_entries (user_id, school_name_raw, deleted_at) "
            "VALUES (:u, 'LIVE', NULL), (:u, 'DELETED', now())"
        ),
        {"u": user_id},
    )


async def seed_awards(conn: Any, user_id: UUID) -> None:
    await conn.execute(
        text(
            "INSERT INTO user_awards (user_id, institution, title, deleted_at) "
            "VALUES (:u, 'A Body', 'LIVE', NULL), (:u, 'A Body', 'DELETED', now())"
        ),
        {"u": user_id},
    )


async def seed_mentor_profile(conn: Any, user_id: UUID) -> None:
    """Only the deleted one: `mentor_profiles` is unique per user, so "a live row
    and a deleted row" is not a state this table can hold. The read must return
    `None`, not the deleted row."""
    await conn.execute(
        text(
            "INSERT INTO mentor_profiles (user_id, headline, deleted_at) "
            "VALUES (:u, 'DELETED', now())"
        ),
        {"u": user_id},
    )


async def read_education(session: AsyncSession, user_id: UUID) -> list[str]:
    return [row["school_name_raw"] for row in await list_education(session, user_id)]


async def read_awards(session: AsyncSession, user_id: UUID) -> list[str]:
    return [row["title"] for row in await list_awards(session, user_id)]


async def read_mentor_profile(session: AsyncSession, user_id: UUID) -> list[str]:
    row = await get_mentor_profile(session, user_id)
    return [] if row is None else [str(row["headline"])]


Case = tuple[
    str,
    Callable[[Any, UUID], Awaitable[None]],
    Callable[[AsyncSession, UUID], Awaitable[list[str]]],
    list[str],
]

CASES: list[Case] = [
    ("education_entries", seed_education, read_education, ["LIVE"]),
    ("user_awards", seed_awards, read_awards, ["LIVE"]),
    ("mentor_profiles", seed_mentor_profile, read_mentor_profile, []),
]


def test_every_soft_deletable_table_is_covered() -> None:
    """The half that keeps the list above honest.

    Checking only the declared cases would pass forever against a table nobody
    added — the list would be guarding itself. This derives the requirement from
    the metadata, so adding `deleted_at` to a table read by `profile_store`
    fails here rather than shipping a read that ignores it.
    """
    covered = {name for name, _, _, _ in CASES}

    assert SOFT_DELETABLE, "the metadata walk found nothing — it is not looking where it thinks"
    assert covered == SOFT_DELETABLE - EXEMPT, (
        f"soft-deletable but unchecked: {sorted(SOFT_DELETABLE - EXEMPT - covered)}; "
        f"checked but no longer soft-deletable: {sorted(covered - SOFT_DELETABLE)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "seed", "read", "expected"), CASES, ids=[case[0] for case in CASES]
)
async def test_a_soft_deleted_row_is_not_returned(
    db_engine: AsyncEngine,
    table: str,
    seed: Callable[[Any, UUID], Awaitable[None]],
    read: Callable[[AsyncSession, UUID], Awaitable[list[str]]],
    expected: list[str],
) -> None:
    async with db_engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES ('soft@example.com', 'mentee', 'UTC') RETURNING id"
                )
            )
        ).scalar_one()
        await seed(conn, user_id)

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        assert await read(session, user_id) == expected, f"{table} returned a deleted row"

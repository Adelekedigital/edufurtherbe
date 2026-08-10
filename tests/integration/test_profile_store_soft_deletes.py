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
from conftest import PROJECT_ROOT

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

#: Exempt only because **nothing reads them yet**. M3 PR 1 ships the availability
#: schema and no read path at all; the endpoints arrive in PR 4, and that is when
#: a case belongs here.
#:
#: This is the dangerous kind of exemption — the kind that silently becomes a
#: gap the moment somebody adds the reader it was waiting for. So it is not taken
#: on trust: `test_the_unread_exemption_expires_when_a_reader_appears` fails as
#: soon as any store module names one of these tables, which forces the choice
#: back into the open instead of leaving it to whoever remembers this comment.
EXEMPT_UNTIL_READ = {"availability_rules", "availability_exceptions"}


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
    exempt = EXEMPT | EXEMPT_UNTIL_READ

    assert SOFT_DELETABLE, "the metadata walk found nothing — it is not looking where it thinks"
    assert covered == SOFT_DELETABLE - exempt, (
        f"soft-deletable but unchecked: {sorted(SOFT_DELETABLE - exempt - covered)}; "
        f"checked but no longer soft-deletable: {sorted(covered - SOFT_DELETABLE)}"
    )


def test_the_unread_exemption_expires_when_a_reader_appears() -> None:
    """`EXEMPT_UNTIL_READ` is only honest while the premise holds.

    The premise is "nothing reads these tables", and it stops being true in a
    pull request that has no reason to look at this file. Rather than trusting
    that, the premise is asserted: a store module naming one of these tables
    fails here, and the fix is to add a case above and shrink the set.

    Scoped to the store modules — `models/` names every table by definition, and
    a migration is a write path rather than a read.
    """
    store_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "src" / "app" / "infra" / "db").glob("*.py")
    )

    assert store_source, "no store modules read; this assertion would pass on an empty string"

    leaked = {table for table in EXEMPT_UNTIL_READ if table in store_source}
    assert not leaked, (
        f"{sorted(leaked)} now has a reader in infra/db, so the "
        f"'nothing reads it yet' exemption no longer holds. Add a case to CASES "
        f"and remove it from EXEMPT_UNTIL_READ."
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

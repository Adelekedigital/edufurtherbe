"""What `feature_interest` guarantees, and what no gate can see (#365).

`alembic check` reads tables, columns, types and regular indexes. The three
things this table leans on are outside that set: a `CHECK` carrying a regex, a
composite `UNIQUE` that is the whole of its idempotency, and a partial index. A
green migration check says the chain applied, not that any of this holds.

**The `CHECK` is pinned to the domain here, not only in the unit test.** That
test compares `FEATURE_PATTERN` to the ORM model's declaration; this one compares
it to what the *migration actually produced*, which is the copy a running
deployment enforces. The two are different claims: a model could declare one
pattern while the column enforces another, and nothing else would notice.

Every constraint gets a rejecting **and** an accepting case, because a test that
only proves a constraint refuses garbage cannot tell a working constraint from
one that refuses everything.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_mentor_status_log import make_user

from app.domain.interest import FEATURE_PATTERN

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_person(engine: AsyncEngine, tag: str) -> UUID:
    return await make_user(engine, uuid4(), f"{tag}@example.com")


async def register(engine: AsyncEngine, user_id: UUID, feature: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO feature_interest (user_id, feature) VALUES (:u, :f)"),
            {"u": user_id, "f": feature},
        )


async def test_the_column_enforces_the_domain_s_pattern(db_engine: AsyncEngine) -> None:
    """**The copy a deployment actually enforces.**

    Watched to fail by widening `FEATURE_PATTERN`: the constraint the migration
    created still says 40 characters, and this names the mismatch rather than
    leaving it to be found when an insert 500s.
    """
    async with db_engine.connect() as conn:
        definition = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_feature_interest_feature_is_a_slug'"
                )
            )
        ).scalar_one()

    assert FEATURE_PATTERN in definition


@pytest.mark.parametrize("feature", ["payments", "new_mentors", "ab", "a" * 40])
async def test_a_well_formed_key_is_stored(db_engine: AsyncEngine, feature: str) -> None:
    person = await a_person(db_engine, f"ok-{feature[:12]}")

    await register(db_engine, person, feature)

    async with db_engine.connect() as conn:
        kept = (
            (
                await conn.execute(
                    text("SELECT feature FROM feature_interest WHERE user_id = :u"),
                    {"u": person},
                )
            )
            .scalars()
            .all()
        )
    assert kept == [feature]


@pytest.mark.parametrize(
    ("case", "feature"),
    [
        ("empty", ""),
        ("one-char", "a"),
        ("over-length", "a" * 41),
        ("upper-case", "Payments"),
        ("leading-digit", "1payments"),
        ("hyphen", "pay-ments"),
    ],
)
async def test_a_malformed_key_is_refused_by_the_column(
    db_engine: AsyncEngine, case: str, feature: str
) -> None:
    """Defence in depth: the API validates first, and `scripts/` can insert
    without passing through it at all."""
    person = await a_person(db_engine, f"bad-{case}")

    with pytest.raises(IntegrityError, match="feature_is_a_slug"):
        await register(db_engine, person, feature)


async def test_pressing_twice_cannot_make_two_rows(db_engine: AsyncEngine) -> None:
    """**The uniqueness is the idempotency**, which is why there is no
    `Idempotency-Key` on the write: a second press conflicts and does nothing,
    rather than being de-duplicated by a header the client has to remember."""
    person = await a_person(db_engine, "presses-twice")
    await register(db_engine, person, "payments")

    with pytest.raises(IntegrityError, match="uq_feature_interest_user_feature"):
        await register(db_engine, person, "payments")


async def test_two_people_may_wait_for_the_same_feature(db_engine: AsyncEngine) -> None:
    """The accepting case for that constraint. Without it, a `UNIQUE (feature)`
    typo would pass the test above while letting exactly one person ever ask."""
    first = await a_person(db_engine, "waits-first")
    second = await a_person(db_engine, "waits-second")

    await register(db_engine, first, "payments")
    await register(db_engine, second, "payments")

    async with db_engine.connect() as conn:
        waiting = (
            await conn.execute(
                text("SELECT count(*) FROM feature_interest WHERE feature = 'payments'")
            )
        ).scalar_one()
    assert waiting == 2


async def test_one_person_may_wait_for_several_features(db_engine: AsyncEngine) -> None:
    """The other accepting case: the constraint is on the pair, not the person."""
    person = await a_person(db_engine, "waits-for-two")

    await register(db_engine, person, "payments")
    await register(db_engine, person, "new_mentors")

    async with db_engine.connect() as conn:
        kept = (
            (
                await conn.execute(
                    text(
                        "SELECT feature FROM feature_interest WHERE user_id = :u ORDER BY feature"
                    ),
                    {"u": person},
                )
            )
            .scalars()
            .all()
        )
    assert kept == ["new_mentors", "payments"]


async def test_deleting_the_account_takes_its_interests(db_engine: AsyncEngine) -> None:
    """`ON DELETE CASCADE`, matching `calendar_connections`.

    An interest is a standing request to be contacted; a deleted account cannot
    be, and a row left behind is a promise nobody can keep — and one that would
    block the delete outright under `RESTRICT`.
    """
    person = await a_person(db_engine, "deleted-account")
    await register(db_engine, person, "payments")

    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": person})

    async with db_engine.connect() as conn:
        left = (
            await conn.execute(
                text("SELECT count(*) FROM feature_interest WHERE user_id = :u"),
                {"u": person},
            )
        ).scalar_one()
    assert left == 0

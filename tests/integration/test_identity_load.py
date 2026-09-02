"""Loading ``users`` from a legacy snapshot, with Bubble's timestamps intact.

The timestamp tests are the reason this file exists. Everything else here fails
loudly if it breaks — a bad role hits the enum, a duplicate email hits the
partial unique index. A load that ran with ``trg_set_updated_at`` enabled fails
nothing at all: every row is present, every count agrees, and every
``updated_at`` is the moment the ETL ran.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.transform import TransformError, to_user, transform_users
from app.infra.etl.loader import UserLoader
from app.infra.etl.reconcile import reconcile_users

pytestmark = pytest.mark.db

CREATED = "2023-12-07T18:36:46.179Z"
MODIFIED = "2025-11-06T11:52:41.383Z"


def record(**overrides: Any) -> dict[str, Any]:
    """A canonical record, as the M1b reader emits one."""
    base: dict[str, Any] = {
        "bubble_id": "1701974206179x877854702892984200",
        "email": "sadeleke2019@gmail.com",
        "👥Role": "Mentor",
        "UserTimezonID": "America/New_York",
        "created_at": CREATED,
        "modified_at": MODIFIED,
        "First Name": "Sakiratu",
        "Last Name": "Adeleke",
        "Slug": "sakiratu-adeleke",
    }
    return base | overrides


# --------------------------------------------------------------------------
# the timestamp guarantee
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bubble_timestamps_survive_the_load(db_engine: AsyncEngine) -> None:
    """Settled decision #29, and the assertion the whole loader is built around.

    ``created_at`` is Bubble's Creation Date and ``updated_at`` its Modified
    Date.

    **The load runs twice, deliberately.** ``trg_set_updated_at`` is a
    ``BEFORE UPDATE`` trigger, so it never fires on the first load and this
    assertion would pass against a loader that disables nothing. Only the second
    load takes the upsert's ``DO UPDATE`` branch, which is where the trigger
    would overwrite Bubble's Modified Date with the import clock — and since the
    ETL must be idempotent, that is the ordinary path rather than an edge case.

    Verified by deleting the ``DISABLE`` and re-running: with a single load this
    test stayed green.
    """
    rows = transform_users([record()]).rows

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)

        stored = await conn.execute(
            text("SELECT created_at, updated_at FROM users WHERE legacy_bubble_id = :b"),
            {"b": rows[0].legacy_bubble_id},
        )
        created_at, updated_at = stored.one()

    assert created_at == datetime(2023, 12, 7, 18, 36, 46, 179000, tzinfo=UTC)
    assert updated_at == datetime(2025, 11, 6, 11, 52, 41, 383000, tzinfo=UTC)


@pytest.mark.asyncio
async def test_the_trigger_is_re_enabled_after_a_successful_load(db_engine: AsyncEngine) -> None:
    """Leaving it disabled is a worse failure than the one it was turned off for.

    Every later application write would stop maintaining ``updated_at``, with
    nothing to indicate why — and the schema test that asserts the trigger
    *exists* would still pass, because a disabled trigger is still attached.
    """
    async with db_engine.begin() as conn:
        await UserLoader(conn).load(transform_users([record()]).rows)

        enabled = await conn.execute(
            text(
                "SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_set_updated_at' "
                "AND tgrelid = 'users'::regclass"
            )
        )
        # `tgenabled` is a `"char"` column, which asyncpg returns as bytes.
        assert enabled.scalar_one() == b"O", "trigger left disabled after the load"


@pytest.mark.asyncio
async def test_the_trigger_is_re_enabled_even_when_the_load_fails(db_engine: AsyncEngine) -> None:
    """The failure path, and it is the rollback that does the work.

    A ``try/finally`` here was written first and is wrong: ``DISABLE TRIGGER`` is
    transactional, so a failed load rolls it back anyway, and the ``finally``
    instead fires inside an aborted transaction — failing to re-enable anything
    and replacing the constraint violation with ``InFailedSQLTransactionError``.
    This test is what caught that, which is why it asserts the *original* error
    is still visible as well as the trigger being restored.
    """
    # Two different ids sharing one email: the upsert keys on legacy_bubble_id,
    # so the second insert violates the partial unique index mid-load.
    clash = transform_users([record(), record(bubble_id="2x2", **{"Slug": "other-slug"})]).rows
    assert len(clash) == 2, "the fixture must actually produce two rows"

    async with db_engine.connect() as conn:
        async with conn.begin():
            with pytest.raises(IntegrityError, match="ix_users_email_live"):
                await UserLoader(conn).load(clash)
            # The rollback is what restores the trigger — not a `finally`. This
            # asserts the real mechanism rather than a cleanup step.

        enabled = await conn.execute(
            text(
                "SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_set_updated_at' "
                "AND tgrelid = 'users'::regclass"
            )
        )
        assert enabled.scalar_one() == b"O", "the rollback did not restore the trigger"

        remaining = await conn.execute(text("SELECT count(*) FROM users"))
        assert remaining.scalar_one() == 0, "a failed load must leave nothing behind"


@pytest.mark.asyncio
async def test_an_ordinary_update_after_the_load_moves_updated_at(db_engine: AsyncEngine) -> None:
    """The counterweight.

    Asserting only that migrated timestamps survive would also pass against a
    loader that dropped the trigger permanently. This is what distinguishes
    "held off during the load" from "turned off".
    """
    rows = transform_users([record()]).rows

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET first_name = 'Edited' WHERE legacy_bubble_id = :b"),
            {"b": rows[0].legacy_bubble_id},
        )
        after = await conn.execute(
            text("SELECT updated_at FROM users WHERE legacy_bubble_id = :b"),
            {"b": rows[0].legacy_bubble_id},
        )

    assert after.scalar_one() > rows[0].updated_at


# --------------------------------------------------------------------------
# idempotence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_load_changes_nothing(db_engine: AsyncEngine) -> None:
    """The guardrail: importers are idempotent on ``legacy_bubble_id``.

    A rehearsal that cannot be repeated is a rehearsal you get to do once.
    """
    rows = transform_users([record()]).rows

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        first = await conn.execute(text("SELECT id, created_at, updated_at FROM users"))
        before = first.all()

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        second = await conn.execute(text("SELECT id, created_at, updated_at FROM users"))

    assert second.all() == before


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_passes_on_a_clean_load(db_engine: AsyncEngine) -> None:
    rows = transform_users(
        [record(), record(bubble_id="2x2", email="b@example.com", **{"Slug": "other-slug"})]
    ).rows

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        result = await reconcile_users(conn, rows)

    assert result.ok, result.report()
    assert result.loaded == 2


@pytest.mark.asyncio
async def test_reconciliation_catches_a_load_that_left_the_trigger_on(
    db_engine: AsyncEngine,
) -> None:
    """The check that justifies reconciliation existing at all.

    Written by loading correctly and then letting the trigger stamp the row, which
    is precisely what a loader missing its ``DISABLE`` would produce. Counts
    match, nothing is missing, no column is null — and reconciliation still fails.
    """
    rows = transform_users([record()]).rows

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        # The trigger is back on by now, so a plain UPDATE re-stamps updated_at
        # exactly as an undisabled load would have.
        await conn.execute(text("UPDATE users SET first_name = first_name"))

        result = await reconcile_users(conn, rows)

    assert not result.ok
    assert result.loaded == result.expected, "count is right; only the timestamp is wrong"
    assert not result.missing
    assert result.wrong_updated_at == (rows[0].legacy_bubble_id,)
    assert "trg_set_updated_at" in result.report()


@pytest.mark.asyncio
async def test_reconciliation_catches_a_row_that_did_not_land(db_engine: AsyncEngine) -> None:
    rows = transform_users(
        [record(), record(bubble_id="2x2", email="b@example.com", **{"Slug": "other-slug"})]
    ).rows

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows[:1])
        result = await reconcile_users(conn, rows)

    assert not result.ok
    assert result.missing == ("2x2",)


# --------------------------------------------------------------------------
# the transform's refusals, against the real schema
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transformed_role_matches_the_database_enum(db_engine: AsyncEngine) -> None:
    """Guards the mapping against the enum drifting apart from it.

    ``PRIMARY_ROLES`` maps legacy strings to ``PrimaryRole``; nothing else
    checks that those members are still the type's labels.
    """
    rows = transform_users([record(**{"👥Role": "Mentee"})]).rows

    async with db_engine.begin() as conn:
        await UserLoader(conn).load(rows)
        stored = await conn.execute(text("SELECT primary_role::text FROM users"))

    assert stored.scalar_one() == "mentee"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("👥Role", "Coach", "unmapped role"),
        ("UserTimezonID", "Mars/Olympus", "not an IANA zone"),
        ("Slug", "Not-URL-Safe", "not URL-safe"),
        ("email", None, "no email"),
    ],
)
def test_an_unmappable_value_raises_rather_than_defaulting(
    field: str, value: Any, expected: str
) -> None:
    """No database needed — the point is that these never reach one.

    Each of these has a plausible silent default: ``mentee``, ``UTC``, dropping
    the slug, a synthetic address. Every one would produce rows that look normal
    and are wrong.
    """
    with pytest.raises(TransformError, match=expected):
        to_user(record(**{field: value}))


def test_duplicate_emails_are_found_before_the_load() -> None:
    """The partial unique index catches these too — halfway through a load, with
    rows already written. Finding them first is what the runbook asks for."""
    report = transform_users([record(), record(bubble_id="2x2")])

    assert not report.ok
    assert report.duplicate_emails == {
        "sadeleke2019@gmail.com": ("1701974206179x877854702892984200", "2x2")
    }


def test_a_clean_extract_reports_ok() -> None:
    """Otherwise every assertion above would pass against a report that is never
    ``ok`` for some unrelated reason."""
    report = transform_users(
        [record(), record(bubble_id="2x2", email="b@example.com", **{"Slug": "other-slug"})]
    )

    assert report.ok, report.errors
    assert len(report.rows) == 2

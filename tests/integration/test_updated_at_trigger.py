"""``set_updated_at()`` behaviour, including the migration-safety guard.

The second test is the one that matters. This project's ETL must be idempotent on
``legacy_bubble_id``, so re-running an importer issues UPDATEs, and the stock
version of this trigger would overwrite every migrated row's Bubble modified date
with the importer's clock — silently, with row counts and null-rate reconciliation
both still passing.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LEGACY_TIMESTAMP = datetime(2019, 5, 5, 10, 0, 0, tzinfo=UTC)

# Two statements, executed separately: asyncpg sends everything through the
# extended query protocol, which permits exactly one command per statement.
# Combining them raises "cannot insert multiple commands into a prepared
# statement", which reads like a syntax error and is not one.
CREATE_PROBE_TABLE = """
CREATE TABLE probe (
  id         uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  note       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
)
"""

ATTACH_PROBE_TRIGGER = """
CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON probe
  FOR EACH ROW EXECUTE FUNCTION set_updated_at()
"""


@pytest_asyncio.fixture
async def probe(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A throwaway table carrying the trigger.

    A real table rather than a TEMP one: a temporary table lives in a session, and
    the connection pool does not guarantee which session a later statement lands
    on. The database is disposable, so the table goes with it.
    """
    async with db_engine.begin() as conn:
        await conn.execute(text(CREATE_PROBE_TABLE))
        await conn.execute(text(ATTACH_PROBE_TRIGGER))
        yield conn


async def insert_legacy_row(conn: AsyncConnection) -> None:
    await conn.execute(
        text("INSERT INTO probe (note, created_at, updated_at) VALUES ('imported', :ts, :ts)"),
        {"ts": LEGACY_TIMESTAMP},
    )


async def read_updated_at(conn: AsyncConnection) -> datetime:
    result = await conn.execute(text("SELECT updated_at FROM probe"))
    return result.scalar_one()


async def test_an_ordinary_update_stamps_the_current_time(probe: AsyncConnection) -> None:
    """The behaviour every table relies on.

    The row is aged backwards first, deliberately. ``now()`` is transaction-scoped
    in PostgreSQL — it returns the transaction's start time and does not advance
    within it — so a test that inserted and updated in one transaction could not
    tell a working trigger from a broken one.
    """
    await insert_legacy_row(probe)

    await probe.execute(text("UPDATE probe SET note = 'edited in the app'"))

    assert await read_updated_at(probe) > LEGACY_TIMESTAMP


async def test_an_explicit_updated_at_cannot_be_forced(probe: AsyncConnection) -> None:
    """``updated_at`` is our row's metadata, and a caller cannot forge it.

    This is asserted rather than assumed because it has a direct consequence for
    the migration: an importer cannot carry Bubble's modified date across by
    writing it into this column. It must go in ``legacy_modified_at``, which is
    where source-system timestamps belong — the same rule that puts
    ``legacy_bubble_id`` in its own column instead of the primary key.

    A conditional trigger was tried first, so that an explicit write would win.
    It cannot work: an idempotent importer writes the value the row already
    holds, so "the caller changed it" and "the caller never mentioned it" are the
    same state. Anything relying on that distinction is guessing.
    """
    await insert_legacy_row(probe)

    await probe.execute(
        text("UPDATE probe SET note = 'importer re-run', updated_at = :ts"),
        {"ts": LEGACY_TIMESTAMP},
    )

    assert await read_updated_at(probe) > LEGACY_TIMESTAMP

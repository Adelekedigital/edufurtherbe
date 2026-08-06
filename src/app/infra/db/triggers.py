"""Holding a trigger off while a writer supplies its own values.

Lived in ``infra/etl/loader.py`` until the provisioning CLI needed it too.
Nothing about it is ETL — it disables a named trigger on an arbitrary table
inside the caller's transaction — and a general database mechanic filed under a
phase-specific module is one the next author does not find. The failure mode of
not finding it is silent: 1,200 migrated ``updated_at`` values overwritten with
the writer's clock, with every row-count and null-rate check still passing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

TRIGGER = "trg_set_updated_at"


@asynccontextmanager
async def timestamps_from_source(connection: AsyncConnection, table: str) -> AsyncIterator[None]:
    """Hold ``trg_set_updated_at`` off for the duration of a write.

    A context manager rather than two calls, so the pair cannot be half-written
    — and exported rather than private, because every later phase's loader needs
    exactly this and copying two SQL statements around is how one of them ends up
    missing its partner.

    **``ENABLE`` runs on the success path only, and that is deliberate.** The
    obvious shape here is ``try/finally``, and it is wrong: ``ALTER TABLE ...
    DISABLE TRIGGER`` is transactional in PostgreSQL, so a failed write rolls the
    disable back along with everything else. A ``finally`` would instead fire
    inside an already-aborted transaction, raise ``InFailedSQLTransactionError``,
    fail to re-enable anything, **and replace the original exception with a
    misleading one** — hiding the constraint violation that actually stopped the
    write. That was written, tested, and caught here rather than in a rehearsal.

    The correctness of this therefore rests on the caller being **inside a
    transaction**. Outside one, each statement autocommits, a mid-write failure
    leaves the trigger disabled, and nothing puts it back.

    ``DISABLE TRIGGER`` takes an ACCESS EXCLUSIVE lock and requires table
    ownership. Both are fine for the callers that exist: the migration role owns
    the table, the ETL runs during the cutover freeze, and provisioning holds the
    lock for a single ``UPDATE`` with its network call already done.
    """
    if not connection.in_transaction():
        raise RuntimeError(
            "the trigger must be disabled inside a transaction, so a failed write "
            "rolls it back — see this function's docstring"
        )

    await connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {TRIGGER}"))
    yield
    # No `finally`: on failure the rollback restores the trigger, and re-enabling
    # here would raise inside the aborted transaction and mask the real error.
    await connection.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {TRIGGER}"))

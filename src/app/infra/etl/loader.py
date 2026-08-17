"""Writing transformed rows, with Bubble's timestamps intact.

The mechanism that makes that possible — ``timestamps_from_source`` — moved to
``infra/db/triggers.py`` once a second, non-ETL writer needed it. Re-exported
here so this module still reads as the complete account of the problem.

The whole file exists for one problem. ``trg_set_updated_at`` is unconditional
by design — settled decision #29, and a guarded version was tried and withdrawn
because an idempotent importer writes the value a row already holds, so "the
caller changed it" and "the caller never mentioned it" are the same state.

**The exposure is the re-run, not the first load**, and the distinction is worth
being precise about because it makes the bug harder to find rather than easier.
``trg_set_updated_at`` is a ``BEFORE UPDATE`` trigger: it does not fire on
``INSERT``. So a first load into an empty table preserves Bubble's timestamps
whether or not this code disables anything, and a test that loads once and checks
the values passes against a loader with no ``DISABLE`` at all.

It is the *second* load that breaks — the upsert takes its ``DO UPDATE`` branch,
the trigger fires, and every migrated ``updated_at`` becomes the import clock.
Since the ETL is required to be idempotent and re-runnable, and rehearsals are
run twice by the runbook, that is the normal path rather than an edge case.

This was established by deleting the ``DISABLE`` and watching which tests failed:
only the idempotence test did. The timestamp test passed, which is exactly the
false assurance this note exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.transform import UserRow
from app.infra.db.triggers import timestamps_from_source

__all__ = ["UPSERT_USER", "UserLoader", "timestamps_from_source"]

UPSERT_USER = """
INSERT INTO users (
    legacy_bubble_id, email, primary_role, timezone,
    created_at, updated_at, email_verified_at,
    first_name, last_name, slug, last_active_at
) VALUES (
    :legacy_bubble_id, :email, :primary_role, :timezone,
    :created_at, :updated_at, :email_verified_at,
    :first_name, :last_name, :slug, :last_active_at
)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    email             = EXCLUDED.email,
    primary_role      = EXCLUDED.primary_role,
    timezone          = EXCLUDED.timezone,
    created_at        = EXCLUDED.created_at,
    updated_at        = EXCLUDED.updated_at,
    email_verified_at = EXCLUDED.email_verified_at,
    first_name        = EXCLUDED.first_name,
    last_name         = EXCLUDED.last_name,
    slug              = EXCLUDED.slug,
    last_active_at    = EXCLUDED.last_active_at
"""


class UserLoader:
    """Loads ``users``, idempotently, preserving Bubble's timestamps."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def load(self, rows: Sequence[UserRow]) -> int:
        """Upsert every row and return how many were written.

        Idempotent on ``legacy_bubble_id``, which is what makes a failed load
        safe to re-run: the guardrail requires it, and a rehearsal that cannot be
        repeated is a rehearsal you only get to do once.
        """
        if not rows:
            return 0

        async with timestamps_from_source(self._connection, "users"):
            for row in rows:
                parameters = asdict(row)
                # The enum member is a StrEnum, so it is already its own wire
                # value; passing the member itself would send the *name* on some
                # drivers — the exact defect the deleted `pg_enum` helper
                # existed to prevent one
                # layer down.
                parameters["primary_role"] = row.primary_role.value
                await self._connection.execute(text(UPSERT_USER), parameters)

        return len(rows)

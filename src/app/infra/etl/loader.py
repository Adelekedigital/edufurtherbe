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

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.enums import SignupSource
from app.domain.transform import UserRow
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE, restorable
from app.infra.db.slugs import OPEN_HANDLE_SQL, give_missing_slugs
from app.infra.db.triggers import timestamps_from_source

__all__ = ["UPSERT_USER", "UserLoader", "timestamps_from_source"]

UPSERT_USER = f"""
INSERT INTO users (
    legacy_bubble_id, email, primary_role, timezone,
    created_at, updated_at, email_verified_at,
    first_name, last_name, slug, last_active_at, signup_source
) VALUES (
    :legacy_bubble_id, :email, :primary_role, :timezone,
    :created_at, :updated_at, :email_verified_at,
    :first_name, :last_name, :slug, :last_active_at, :signup_source
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
    -- A final handle is published in links: an import never moves it.
    slug              = CASE WHEN {OPEN_HANDLE_SQL}
                             THEN COALESCE(EXCLUDED.slug, users.slug)
                             ELSE users.slug END,
    slug_is_placeholder = (EXCLUDED.slug IS NULL AND users.slug_is_placeholder),
    last_active_at    = EXCLUDED.last_active_at,
    signup_source     = EXCLUDED.signup_source
"""  # noqa: S608  # nosec B608 - only the module constant OPEN_HANDLE_SQL; values are bound


class UserLoader:
    """Loads ``users``, idempotently, preserving Bubble's timestamps."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection
        #: Bubble ids whose legacy slug another account already held, so they
        #: were given their own handle instead; the script names them.
        self.slugs_held_elsewhere: list[str] = []

    async def _held_elsewhere(self, rows: Sequence[UserRow]) -> dict[str, str | None]:
        """Each batch slug another account holds: a live one, or a self-deleted
        one that may still restore (every other handle writer skips those too).
        Maps slug to its holder's Bubble id, ``None`` for an app account."""
        slugs = [row.slug for row in rows if row.slug]
        if not slugs:
            return {}
        held = await self._connection.execute(
            select(User.slug, User.legacy_bubble_id).where(
                User.slug.in_(slugs), or_(LIVE, and_(*restorable(func.now())))
            )
        )
        return {slug: holder for slug, holder in held.all() if slug is not None}

    async def load(self, rows: Sequence[UserRow]) -> int:
        """Upsert every row and return how many were written.

        Idempotent on ``legacy_bubble_id``, which is what makes a failed load
        safe to re-run: the guardrail requires it, and a rehearsal that cannot be
        repeated is a rehearsal you only get to do once.
        """
        if not rows:
            return 0

        held = await self._held_elsewhere(rows)
        async with timestamps_from_source(self._connection, "users"):
            for row in rows:
                parameters = asdict(row)
                if row.slug in held and held[row.slug] != row.legacy_bubble_id:
                    # Taking it would abort the load on the unique index, or
                    # block a restore; `give_missing_slugs` below gives one.
                    parameters["slug"] = None
                    self.slugs_held_elsewhere.append(row.legacy_bubble_id)
                # The enum member is a StrEnum, so it is already its own wire
                # value; passing the member itself would send the *name* on some
                # drivers — the exact defect the deleted `pg_enum` helper
                # existed to prevent one
                # layer down.
                parameters["primary_role"] = row.primary_role.value
                # Every account this loads came from the old platform.
                parameters["signup_source"] = SignupSource.MIGRATED.value
                await self._connection.execute(text(UPSERT_USER), parameters)
            # Every account has a handle (owner, 2026-11-14): a legacy row
            # with no slug gets one now, not at a later backfill run.
            await give_missing_slugs(self._connection, legacy_only=True)

        return len(rows)

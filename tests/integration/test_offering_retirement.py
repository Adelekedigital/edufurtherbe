"""Retiring an offering, now that nothing refuses it.

**This file replaces `test_primary_offering_guard.py`, which is deleted.** That
suite pinned `trg_refuse_retiring_a_primary_offering` — the schema's first
business-rule trigger — which existed because
`mentor_profiles.primary_session_type_id` was declared `ON DELETE RESTRICT` and
nothing ever hard-deletes a session type. Retirement is `deleted_at` or
`is_active = false`, both UPDATEs, and a foreign key never sees an UPDATE. The
guard was what D90 required in place of a key that could not fire.

The pointer is dropped, so the guard has nothing to protect and goes with it.

**The five tests are recorded here rather than dropped because they turned red.**
Each says what would bring it back — the point of a deliberate deletion is that
the next person can tell a retired control from a forgotten one.

- `test_a_primary_offering_cannot_be_soft_deleted` — the `deleted_at` half of
  the guard. Comes back with any column pointing at a `session_types` row whose
  disappearance breaks a read.
- `test_a_primary_offering_cannot_be_deactivated` — the same for `is_active`,
  kept separate because they were two clauses and either could be deleted with
  the other still passing.
- `test_the_same_offering_can_be_retired_once_it_is_not_the_primary` — a guard
  must be a guard, not a lock. It caught a trigger refusing *every* retirement,
  which would pass both tests above and make an offering permanent.
- `test_a_non_primary_offering_is_untouched_by_the_guard` — over-reach. The
  trigger fired on every `session_types` UPDATE, so a predicate catching rows it
  was never meant to was the cheap mistake.
- `test_an_unrelated_update_still_works` — renaming is not retiring. A guard
  written as "refuse any UPDATE while pointed at" would pass everything above and
  make the row uneditable.

**What replaces them is the inverse**, and it is not ceremony: the absence of a
refusal is exactly what the endpoints in PRs 15 and 16 are built on. Without an
assertion here, a trigger reintroduced by a later migration would make
`PATCH`-ing `is_active` return a 500 and nothing would say why — which is the
failure the guard's own tests were originally written to prevent, pointing the
other way.

Both retirement paths are asserted separately, for the same reason the guard
tested them separately: they were two clauses, and a single test would pass with
one of them resurrected.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def make_mentor_with_offering(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    """A mentor and one session type — the state the guard used to protect."""
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Ada', 'mentor', 'UTC') RETURNING id"
                ),
                {"e": f"retire-{tag}-{uuid4()}@example.test"},
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
    return user_id, session_type_id


async def test_a_mentors_only_offering_can_be_soft_deleted(db_engine: AsyncEngine) -> None:
    """No refusal, where the guard raised `restrict_violation`.

    The mentor's *only* offering, which is the case the guard always caught:
    under D88 the loader made a mentor's first offering their primary, so this
    row was pointed at by construction.
    """
    _, session_type_id = await make_mentor_with_offering(db_engine, "softdelete")

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET deleted_at = now() WHERE id = :s"),
            {"s": session_type_id},
        )

    async with db_engine.connect() as conn:
        deleted = (
            await conn.execute(
                text("SELECT deleted_at FROM session_types WHERE id = :s"), {"s": session_type_id}
            )
        ).scalar_one()
    assert deleted is not None


async def test_a_mentors_only_offering_can_be_deactivated(db_engine: AsyncEngine) -> None:
    """The second path, and the one PR 15's `PATCH` depends on.

    `is_active` is a bare `PATCH` field with no cascade. While the guard existed
    it fired on `UPDATE` too, so this toggle needed the same `409` mapping as
    `DELETE` or it returned a 500 — a translation the endpoint no longer has to
    carry.
    """
    _, session_type_id = await make_mentor_with_offering(db_engine, "deactivate")

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET is_active = false WHERE id = :s"),
            {"s": session_type_id},
        )

    async with db_engine.connect() as conn:
        active = (
            await conn.execute(
                text("SELECT is_active FROM session_types WHERE id = :s"), {"s": session_type_id}
            )
        ).scalar_one()
    assert active is False


async def test_no_trigger_remains_on_session_types_for_retirement(
    db_engine: AsyncEngine,
) -> None:
    """Asked of the catalogue, not inferred from behaviour passing.

    The two tests above would also pass against a trigger whose predicate had
    quietly stopped matching — a rewritten `EXISTS` over a dropped column, say,
    which raises nothing and guards nothing. This asserts the object is *gone*,
    which is the claim the migration actually makes.

    `trg_set_updated_at` is expected and is asserted present rather than filtered
    out, so a migration that dropped the wrong trigger fails here too.
    """
    async with db_engine.connect() as conn:
        triggers = sorted(
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        " WHERE tgrelid = 'session_types'::regclass AND NOT tgisinternal"
                    )
                )
            ).all()
        )

    assert triggers == ["trg_set_updated_at"]

"""Did the load actually do what it claimed?

The runbook's rule: a phase is not done when it loads without error, it is done
when reconciliation passes. Loading without error is a low bar — every check the
schema enforces has already run by then, so what is left is exactly the class of
defect the schema cannot see.

Three checks, and the third is the one worth having:

1. **Counts match.** Cheapest, weakest. Catches a truncated read.
2. **Every source row landed.** Catches a silently skipped record, which a count
   would miss if anything else was double-written.
3. **Timestamps equal the source.** The only check that can detect a load which
   ran with ``trg_set_updated_at`` still enabled — where every row is present,
   every count agrees, every column is populated, and every ``updated_at`` is
   the import clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.transform import UserRow


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the database says, against what was meant to go in."""

    expected: int
    loaded: int
    missing: tuple[str, ...] = ()
    wrong_created_at: tuple[str, ...] = ()
    wrong_updated_at: tuple[str, ...] = ()
    null_email: int = 0
    null_first_name: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def report(self) -> str:
        lines = [
            f"expected {self.expected}, loaded {self.loaded}",
            f"missing: {len(self.missing)}",
            f"created_at differs from source: {len(self.wrong_created_at)}",
            f"updated_at differs from source: {len(self.wrong_updated_at)}",
            f"null email: {self.null_email}   null first_name: {self.null_first_name}",
        ]
        lines += [f"FAIL {problem}" for problem in self.problems]
        return "\n".join(lines)


async def reconcile_users(connection: AsyncConnection, rows: Sequence[UserRow]) -> Reconciliation:
    """Compare the loaded ``users`` against the rows that were meant to land."""
    expected = {row.legacy_bubble_id: row for row in rows}

    result = await connection.execute(
        text(
            "SELECT legacy_bubble_id, created_at, updated_at, email, first_name "
            "FROM users WHERE legacy_bubble_id IS NOT NULL"
        )
    )
    loaded = {r.legacy_bubble_id: r for r in result}

    missing = tuple(sorted(set(expected) - set(loaded)))

    # Compared to the microsecond. A load that ran with the trigger enabled
    # produces `updated_at` values within a second or two of each other and of
    # now(), so a tolerant comparison would pass exactly the failure this exists
    # to catch.
    wrong_created = tuple(
        sorted(
            key
            for key, row in expected.items()
            if key in loaded and loaded[key].created_at != row.created_at
        )
    )
    wrong_updated = tuple(
        sorted(
            key
            for key, row in expected.items()
            if key in loaded and loaded[key].updated_at != row.updated_at
        )
    )

    null_email = sum(1 for row in loaded.values() if not row.email)
    null_first_name = sum(1 for row in loaded.values() if not row.first_name)

    problems: list[str] = []
    if len(loaded) != len(expected):
        problems.append(f"count mismatch: expected {len(expected)}, found {len(loaded)}")
    if missing:
        problems.append(f"{len(missing)} source rows did not land, first: {missing[:5]}")
    if wrong_created:
        problems.append(f"{len(wrong_created)} rows have a created_at the source did not supply")
    if wrong_updated:
        problems.append(
            f"{len(wrong_updated)} rows have an updated_at the source did not supply — "
            f"the load probably ran with {'trg_set_updated_at'} enabled"
        )
    if null_email:
        problems.append(f"{null_email} rows have no email")

    return Reconciliation(
        expected=len(expected),
        loaded=len(loaded),
        missing=missing,
        wrong_created_at=wrong_created,
        wrong_updated_at=wrong_updated,
        null_email=null_email,
        null_first_name=null_first_name,
        problems=problems,
    )

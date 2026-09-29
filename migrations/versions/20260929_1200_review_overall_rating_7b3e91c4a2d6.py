"""``reviews.overall_rating``: step 1's stars, and the card index that covers them.

The frontend's review form gains "Your rating", 1 to 5 (2026-09-29), separate from
``valuable_rating``. The column is **nullable with no default**: every review
written before this has no overall rating, and none is invented — the session
value reads those through ``valuable_rating`` instead (``review_stats``).

Nullable and default-free, so adding it is metadata-only and old code, which
never names it, runs unchanged against the new table. The ``CHECK`` is added
``NOT VALID`` and then validated, **but in the same transaction**, so the
validating scan does run while the ``ACCESS EXCLUSIVE`` lock from the ``ALTER``
is still held. That is accepted rather than split out: the column is null on
every row, so there is nothing to find, and ``reviews`` is tens of thousands
of rows — a short scan, bounded by ``lock_timeout`` getting the lock at all.

**Re-runnable after a failed index swap.** ``autocommit_block`` commits the
column before the concurrent build starts, so a build that fails leaves the
column in place with the revision unstamped. The column and the constraint are
therefore added only if absent, and the swap clears any half-built index first.

**The card index is rebuilt to carry the column.** The card's session value
reads ``overall_rating`` beside ``valuable_rating`` from now on, and
``ix_reviews_mentor_valuable`` covering only the latter would turn an index-only
scan per card into a heap scan per card. Built concurrently under a temporary
name, the old one dropped concurrently, then renamed — so the name every test
and docstring uses survives and there is no moment without an index.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b3e91c4a2d6"
down_revision: str | Sequence[str] | None = "ebf9319030f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "reviews"
CHECK = "ck_reviews_overall_rating_range"
INDEX = "ix_reviews_mentor_valuable"
BUILDING = "ix_reviews_mentor_valuable_next"
PREDICATE = "deleted_at IS NULL AND reviewed_for_role = 'mentor'"

#: A literal rather than an f-string: the names are fixed, and it keeps the one
#: DO block free of interpolation. Pinned to `CHECK` below so the two cannot drift.
_ADD_CHECK = """
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_reviews_overall_rating_range'
  ) THEN
    ALTER TABLE reviews ADD CONSTRAINT ck_reviews_overall_rating_range
      CHECK (overall_rating BETWEEN 1 AND 5) NOT VALID;
  END IF;
END $$;
"""
if CHECK not in _ADD_CHECK:  # pragma: no cover - a module that disagrees with itself
    raise RuntimeError(f"_ADD_CHECK does not create {CHECK}")


def _swap_index(columns: str) -> None:
    """Replace the card index with one over ``columns``, never going without.

    ``IF EXISTS``/``IF NOT EXISTS`` throughout, because a failed ``CONCURRENTLY``
    build leaves an ``INVALID`` index behind and a re-run must get past it.
    """
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {BUILDING}")
        op.execute(f"CREATE INDEX CONCURRENTLY {BUILDING} ON {TABLE} ({columns}) WHERE {PREDICATE}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(f"ALTER INDEX {BUILDING} RENAME TO {INDEX}")


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS overall_rating smallint")
    op.execute(_ADD_CHECK)
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK}")
    _swap_index("reviewed_for, valuable_rating, overall_rating")


def downgrade() -> None:
    """Drop the column. **Every overall rating written since is lost** — the
    stars exist nowhere else. Roll forward rather than back once real reviews
    carry them."""
    _swap_index("reviewed_for, valuable_rating")
    op.execute("SET lock_timeout = '3s'")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS overall_rating")

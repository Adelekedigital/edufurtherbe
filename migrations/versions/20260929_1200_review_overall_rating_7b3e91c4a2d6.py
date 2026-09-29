"""``reviews.overall_rating``: step 1's stars, and the card index that covers them.

The frontend's review form gains "Your rating", 1 to 5 (2026-09-29), separate from
``valuable_rating``. The column is **nullable with no default**: every review
written before this has no overall rating, and none is invented — the session
value reads those through ``valuable_rating`` instead (``review_stats``).

Nullable and default-free, so adding it is metadata-only and old code, which
never names it, runs unchanged against the new table. The ``CHECK`` is added
``NOT VALID`` and validated separately, so no scan runs under the
``ACCESS EXCLUSIVE`` lock the ``ALTER`` takes — though over a column that is
null on every row the validation has nothing to find.

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
down_revision: str | Sequence[str] | None = "d5c69f3e05ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "reviews"
CHECK = "ck_reviews_overall_rating_range"
INDEX = "ix_reviews_mentor_valuable"
BUILDING = "ix_reviews_mentor_valuable_next"
PREDICATE = "deleted_at IS NULL AND reviewed_for_role = 'mentor'"


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
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN overall_rating smallint")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} "
        "CHECK (overall_rating BETWEEN 1 AND 5) NOT VALID"
    )
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK}")
    _swap_index("reviewed_for, valuable_rating, overall_rating")


def downgrade() -> None:
    """Drop the column. **Every overall rating written since is lost** — the
    stars exist nowhere else. Roll forward rather than back once real reviews
    carry them."""
    _swap_index("reviewed_for, valuable_rating")
    op.execute("SET lock_timeout = '3s'")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN overall_rating")

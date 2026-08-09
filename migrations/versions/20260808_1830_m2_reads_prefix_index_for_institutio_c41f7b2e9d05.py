"""M2 reads - prefix index for institution autocomplete

One index, added because a measurement said so rather than because it seemed
prudent.

WHY THE TRIGRAM INDEX IS NOT ENOUGH
===================================
``ix_institutions_name_trgm`` (GIN, ``gin_trgm_ops``) serves ``ILIKE '%term%'``
and the ``%`` similarity operator. It does **not** serve ``ILIKE 'term%'``.
Autocomplete's first and most common tier is exactly that prefix match — one
query per keystroke — and on the real 10,250-row catalogue it measures:

    name ILIKE 'University%'    59.3 ms   without this index (sequential scan)
                                 4.3 ms   with it
    name ILIKE 'La%'            36.5 ms / 2.6 ms
    name ILIKE 'Lagos%'          2.8 ms / 1.4 ms

So this is the common path, not an edge case. The expensive trigram tier runs
only when prefix and substring both come back short, which is the typo path.

WHY `text_pattern_ops` AND WHY `lower(name)`
============================================
A default btree sorts by the database collation, which is not character order,
so it cannot answer ``LIKE 'x%'``. ``text_pattern_ops`` is the operator class
that can. The expression is ``lower(name)`` because the search is
case-insensitive, and **the query must spell the expression the same way** —
``WHERE lower(name) LIKE lower(:q) || '%'``. An index on ``name`` with a query on
``lower(name)`` is silently unused, which is the failure mode worth naming here:
nothing errors, the index simply never gets picked and the endpoint is slow.

Revision ID: c41f7b2e9d05
Revises: 13688233ac14
Create Date: 2026-08-08 18:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c41f7b2e9d05"
down_revision: str | Sequence[str] | None = "13688233ac14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE INDEX ix_institutions_name_prefix ON institutions (lower(name) text_pattern_ops)"
    )


def downgrade() -> None:
    """Downgrade schema.

    Reversible without loss — an index carries no data of its own. Dropping it
    makes autocomplete slow, not wrong.
    """
    op.execute("DROP INDEX ix_institutions_name_prefix")

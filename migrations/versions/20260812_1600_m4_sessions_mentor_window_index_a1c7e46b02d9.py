"""An index for the public slots read: every session a mentor holds, in a span.

Revision ID: a1c7e46b02d9
Revises: e9b4d27c6a31
Create Date: 2026-08-12 16:00:00.000000

**Purely additive, and invisible to application code.** An index changes no
result, so old and new code running side by side during a rolling deploy cannot
disagree about anything here. There is no migrate phase and no contract phase.

**Why the three existing per-party indexes do not serve this read.**
`ix_sessions_mentor_upcoming` and `ix_sessions_mentee_upcoming` are partial on
the live statuses and `ix_sessions_mentor_completed` on `completed` — so
`cancelled`, `declined`, `expired` and `no_show` sit in no per-party index at
all. The slots endpoint subtracts a mentor's sessions **whatever their status**,
because a cancelled session keeps its slot until somebody deliberately releases
it. That read therefore matched nothing and scanned the table.

Measured at 20,000 sessions before this was written:

    without the index   Seq Scan, 19,839 rows removed by filter   13.199 ms
    with the index      Bitmap Index Scan on this index            1.255 ms

`gist`, not `btree`, because the predicate is `&&` against a `tstzrange` — a
containment question a btree cannot answer. `btree_gist` is already installed by
the exclusion-constraint migration, which is what lets `mentor_id` (a plain
uuid) share a gist index with a range expression.

**Not partial, deliberately.** A predicate here would reintroduce exactly the
gap this index exists to close.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1c7e46b02d9"
down_revision: str | None = "e9b4d27c6a31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_sessions_mentor_window"


def upgrade() -> None:
    """Build the index without blocking writes to `sessions`.

    **`CONCURRENTLY` here, unlike the identity migration**, whose indexes were on
    tables that same migration had just created — empty, invisible to any other
    session, and cheaper to build inline. `sessions` already holds rows and is
    read and written by live code, so an inline build would take `SHARE` and
    block every write for its duration. A blocked DDL statement in PostgreSQL
    also blocks everything queued behind it, so the failure mode is a table-wide
    stall rather than one slow statement.

    `lock_timeout` fails fast instead of joining that queue. No
    `statement_timeout`: a concurrent build legitimately takes as long as the
    table is large, and killing it halfway is what leaves an `INVALID` index
    behind.
    """
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON sessions "
            "USING gist (mentor_id, session_window(starts_at, duration_minutes))"
        )


def downgrade() -> None:
    """Drop it, also concurrently.

    Fully reversible: no data is stored in an index, so this loses nothing.

    `IF EXISTS` on both sides is not defensive noise — a failed `CONCURRENTLY`
    build leaves an `INVALID` index of this name behind, and a downgrade that
    could not remove it would strand the chain. Check for one after any failure:
    `SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;`
    """
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '3s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")

"""The schema for deleting your own account (Mentee Settings PRD, 2026-10-19).

``DELETE /me`` soft-deletes the account and gives it 30 days before a purge job
removes it for good; an admin may restore it until then. Two changes:

1. ``users.deletion_purge_at``, nullable. Set only by ``DELETE /me``, so a
   migrated soft delete (``deleted_at`` set, no purge date) is never purged by
   the job. ``ck_users_deletion_purge_follows_deleted`` refuses a purge date on
   a live account, or one not after the deletion, either of which the job would
   delete early. A plain ``ADD CONSTRAINT``: ``ADD COLUMN`` already holds
   ``ACCESS EXCLUSIVE`` on ``users`` until commit, so a ``NOT VALID`` split
   would buy nothing, and ``users`` is ~1,200 rows.
2. ``account_deleted`` joins ``ck_session_events_reason_code_is_known``, a
   constraint swap like ``5d8b3e1f7a24`` added ``other``: each session the
   delete ends records why.

Merged alone, before the code that writes either (project-conventions,
*Migration ordering*). Nothing writes them yet, so this lands inert.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b9d2a7c1f3"
down_revision: str | Sequence[str] | None = "c7d4a2e9f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The vocabulary before this migration, for ``downgrade``. The new one lives in
#: ``CONVERSIONS`` below, the single copy ``upgrade`` uses and the pin test reads.
WITHOUT_ACCOUNT_DELETED = (
    "'mentor_unavailable', 'mentee_no_longer_needed', 'scheduling_conflict', "
    "'technical_issue', 'mentor_no_show', 'mentee_no_show', "
    "'expired_no_response', 'rescheduled', 'admin_action', 'other'"
)

#: **The labels are written out, not referenced**: `migration_tuples` parses this
#: with `ast` and reads only constants (see `e7b4d29c3f16`).
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "session_events",
        "reason_code",
        "session_reason_code",
        "'mentor_unavailable', 'mentee_no_longer_needed', 'scheduling_conflict', "
        "'technical_issue', 'mentor_no_show', 'mentee_no_show', "
        "'expired_no_response', 'rescheduled', 'admin_action', 'other', "
        "'account_deleted'",
        None,
    ),
)

#: Bare for `op`, which adds the `ck_users_` prefix; rendered for the message.
PURGE_CHECK = "deletion_purge_follows_deleted"
PURGE_CHECK_RENDERED = "ck_users_deletion_purge_follows_deleted"


def _swap(table: str, column: str, labels: str) -> None:
    """Drop one ``CHECK`` and re-add it with the given vocabulary. Bare names:
    the naming convention adds the ``ck_{table}_`` prefix."""
    op.drop_constraint(f"{column}_is_known", table, type_="check")
    op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("users", sa.Column("deletion_purge_at", sa.TIMESTAMP(timezone=True)))
    op.create_check_constraint(
        PURGE_CHECK,
        "users",
        "deletion_purge_at IS NULL OR (deleted_at IS NOT NULL AND deletion_purge_at > deleted_at)",
    )
    for table, column, _type_name, labels, _default in CONVERSIONS:
        _swap(table, column, labels)


def downgrade() -> None:
    """**Refuse** while an account awaits its purge or an event is coded
    ``account_deleted``. Dropping the column would turn a deletion its owner
    asked for into a migrated soft delete the job never purges, and rewriting the
    code would erase why those sessions ended (the choice ``5d8b3e1f7a24`` made
    for ``other``). Either is an operator's call, made before downgrading.
    """
    op.execute("SET lock_timeout = '3s'")
    pending = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM users WHERE deletion_purge_at IS NOT NULL"))
        .scalar_one()
    )
    if pending:
        raise RuntimeError(
            f"{pending} deleted account(s) still await their purge; downgrading would "
            f"drop their dates (and {PURGE_CHECK_RENDERED}) and keep them forever"
        )
    _swap("session_events", "reason_code", WITHOUT_ACCOUNT_DELETED)
    op.drop_constraint(PURGE_CHECK, "users", type_="check")
    op.drop_column("users", "deletion_purge_at")

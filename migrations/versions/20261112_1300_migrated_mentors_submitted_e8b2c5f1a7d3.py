"""Stamp ``submitted_at`` on mentor profiles loaded from Bubble (#660).

#491's backfill stamped every profile that existed then, because in Bubble
creating a profile was applying. The profile loader never wrote the column, so
a mentor loaded since has it null: a pending applicant missed the admin queue,
and once `decide` refuses an unsubmitted application (#660) no admin could
decide on any of them. The loader now stamps it; this covers rows already
loaded.

Only rows with a ``legacy_bubble_id``: a profile created here with no
submission is a draft and stays one. Stamped with ``created_at``, with
``trg_set_updated_at`` held off, as c7d4a2e9f1b6 did.

Data only; the downgrade does nothing, because the stamped rows cannot be told
apart from ones stamped before, and un-stamping them would hide real
applications.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b2c5f1a7d3"
down_revision: str | Sequence[str] | None = "d4e7a1c95b26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET session_replication_role = replica")
    op.execute(
        "UPDATE mentor_profiles SET submitted_at = created_at "
        "WHERE submitted_at IS NULL AND legacy_bubble_id IS NOT NULL"
    )
    op.execute("SET session_replication_role = DEFAULT")


def downgrade() -> None:
    """Nothing: see the module docstring."""

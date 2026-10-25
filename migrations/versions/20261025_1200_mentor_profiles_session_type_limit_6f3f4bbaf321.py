"""Add ``mentor_profiles.session_type_limit`` (Session Types PRD, founder 2026-10-21).

How many session types support lets this mentor hold. **Expand only**: nullable,
no default and no backfill, so the add is a catalogue change with no table
rewrite. Null means the platform default (5, in the domain), which is every
existing row; nothing reads the column until the code PR that enforces it.

The ``CHECK`` is a sanity bound (1 to 100), not the product rule. Every existing
value is null, so validating it finds nothing to refuse.

The downgrade drops the column, and with it any override support set: those
are re-set by hand, and until then the mentor has the default.

Revision ID: 6f3f4bbaf321
Revises: b7779986392d
Create Date: 2026-10-25 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f3f4bbaf321"
down_revision: str | Sequence[str] | None = "b7779986392d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Bare for `op`, which adds the `ck_mentor_profiles_` prefix; the rendered name
#: is what the database holds, declared so the identifier test finds it verbatim.
CHECK = "session_type_limit_sane"
CHECK_RENDERED = "ck_mentor_profiles_session_type_limit_sane"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("mentor_profiles", sa.Column("session_type_limit", sa.Integer(), nullable=True))
    op.create_check_constraint(
        CHECK,
        "mentor_profiles",
        "session_type_limit IS NULL OR session_type_limit BETWEEN 1 AND 100",
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(CHECK, "mentor_profiles", type_="check")
    op.drop_column("mentor_profiles", "session_type_limit")

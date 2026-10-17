"""What a mentee wants to study: ``mentee_goals.field_of_study``.

Mentee Onboarding v5 (B1), owner 2026-10-17. Onboarding asks "What do you
want to study?" and matching compares the answer with a mentor's course. The
package plans a generic `tags` / `user_tags` table for this, deferred
(`docs/edufurther-migration/docs/04_DEFERRED.md`), so it is text here and
becomes tag rows later (decision 259).

**Nullable, no default, no backfill**: no existing goal has an answer. A CHECK
holds the 1-100 character bound the API enforces, so a blank or an overlong
value cannot arrive by another path (decision 36).

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads the column until the next PR. Adding a nullable column with no
default is a catalogue change; it takes a brief `ACCESS EXCLUSIVE` lock on a
small table and rewrites nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4710b359d1b"
down_revision: str | Sequence[str] | None = "d7b2f4a8c615"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("mentee_goals", sa.Column("field_of_study", sa.Text()))
    op.create_check_constraint(
        op.f("ck_mentee_goals_field_of_study_length"),
        "mentee_goals",
        "field_of_study IS NULL OR char_length(field_of_study) BETWEEN 1 AND 100",
    )


def downgrade() -> None:
    """Drop the column, **refusing while any goal holds a field of study**.

    Dropping it would delete what mentees typed, with nobody choosing that, so
    a downgrade over values stops and says so.
    """
    op.execute("SET lock_timeout = '3s'")
    # Locked before counting, so a goal written between the count and the drop
    # cannot be deleted without the refusal seeing it.
    op.execute("LOCK TABLE mentee_goals IN SHARE ROW EXCLUSIVE MODE")
    held = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM mentee_goals WHERE field_of_study IS NOT NULL"))
        .scalar_one()
    )
    if held:
        raise RuntimeError(
            f"{held} goal(s) hold a field of study; dropping the column would delete them"
        )
    op.drop_constraint(op.f("ck_mentee_goals_field_of_study_length"), "mentee_goals")
    op.drop_column("mentee_goals", "field_of_study")

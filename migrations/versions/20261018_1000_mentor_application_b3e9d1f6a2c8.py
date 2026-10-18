"""A mentor application's submission and its approval being seen; help areas in
sentence case.

Mentor Onboarding PRD, B1 (18 Oct). Two columns on ``mentor_profiles``, both
``timestamptz`` and nullable:

- ``submitted_at``: when the mentor submitted their application. Creating a
  profile stops being an application in the next PR, so a profile saved partway
  through onboarding is no longer in the admin queue.
- ``approved_seen_at``: when an approved mentor first saw the approved screen,
  so it shows once per account on any device.

**Backfilled with ``created_at``**: under the old rule creating a profile *was*
applying (admins were alerted at once), so every existing profile is submitted.
Only an **approved** one is marked seen: a pending one marked seen would never
show the approved screen once approved. The 44 migrated mentors are all
approved, so they count as submitted and seen, as the PRD asks.

The six ``service_offerings.display_name`` values move to sentence case ("&"
becomes "and"), matched by slug. The ETL maps legacy text to slugs, never to a
display name, so it is unaffected.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads the columns until the next PR. Adding nullable columns with no
default rewrites nothing; the backfill touches ~50 rows and leaves `updated_at`
alone. The downgrade restores
the old names and drops the columns, losing only the submission times, which a
re-upgrade backfills again from ``created_at``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e9d1f6a2c8"
down_revision: str | Sequence[str] | None = "a8d2e5c7f391"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# slug -> (old display name, new display name). A literal, never imported
# (decision 105): a later edit must not change what this revision does.
NAMES = {
    "test-preparation": ("Test Preparation", "Test preparation"),
    "document-preparation": ("Document Preparation", "Document preparation"),
    "school-selection": ("School Selection", "School selection"),
    "program-selection": ("Program Selection", "Program selection"),
    "scholarships-financial-aid": (
        "Scholarships & Financial Aid",
        "Scholarships and financial aid",
    ),
    "interview-preparation": ("Interview Preparation", "Interview preparation"),
}


def _rename(index: int) -> None:
    for slug, names in NAMES.items():
        op.execute(
            sa.text(
                "UPDATE service_offerings SET display_name = :name WHERE slug = :slug"
            ).bindparams(name=names[index], slug=slug)
        )


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("mentor_profiles", sa.Column("submitted_at", sa.TIMESTAMP(timezone=True)))
    op.add_column("mentor_profiles", sa.Column("approved_seen_at", sa.TIMESTAMP(timezone=True)))
    # `trg_set_updated_at` held off, as in `f2a8c31b7e45`: `updated_at` carries
    # Bubble's Modified Date and feeds the featured pick's freshness, so a
    # backfill must not move every mentor to the migration clock.
    op.execute("SET session_replication_role = replica")
    op.execute("UPDATE mentor_profiles SET submitted_at = created_at")
    op.execute(
        "UPDATE mentor_profiles SET approved_seen_at = created_at "
        "WHERE approval_status = 'approved'"
    )
    _rename(1)
    op.execute("SET session_replication_role = DEFAULT")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET session_replication_role = replica")
    _rename(0)
    op.execute("SET session_replication_role = DEFAULT")
    op.drop_column("mentor_profiles", "approved_seen_at")
    op.drop_column("mentor_profiles", "submitted_at")

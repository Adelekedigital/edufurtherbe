"""Stamp ``submitted_at`` on profiles that applied under the old rule after #491.

Mentor Onboarding PRD, B3. #491's backfill stamped every profile that existed
then. Until B2 (#492) deployed, creating a profile still alerted admins, which
was applying, but left ``submitted_at`` null. B3's admin queue lists only
submitted applications, so those profiles would drop out of it unreviewed.

A profile counts as having applied when **either** it was created before #492
merged (``B2_MERGED``, when creating still meant applying, alert or not: no
alert went out when nobody could decide it), **or** its creation enqueued
``mentor_application_received``, which covers the minutes between that merge
and the deploy. Under B2 only a submit alerts, and it sets the column, so a
draft saved since matches neither. **Any approval status**, as #491's backfill:
an application decided in the window was still submitted. Stamped with
``created_at``, with ``trg_set_updated_at`` held off for the same reason.

Data only; the downgrade does nothing, because the stamped rows cannot be told
apart from ones the backfill stamped and un-stamping them would hide real
applications.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d4a2e9f1b6"
down_revision: str | Sequence[str] | None = "b3e9d1f6a2c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: When #492 (B2) merged: before it, creating a mentor profile was applying.
B2_MERGED = "2026-10-18T20:11:48Z"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET session_replication_role = replica")
    op.execute(
        sa.text(
            """
            UPDATE mentor_profiles m SET submitted_at = m.created_at
            WHERE m.submitted_at IS NULL
              AND (
                  m.created_at < CAST(:merged AS timestamptz)
                  OR EXISTS (
                      SELECT 1 FROM outbox_events o
                      WHERE o.event_type = 'mentor_application_received'
                        AND o.entity_type = 'mentor_profile'
                        AND o.entity_id = m.user_id
                  )
              )
            """
        ).bindparams(merged=B2_MERGED)
    )
    op.execute("SET session_replication_role = DEFAULT")


def downgrade() -> None:
    """Nothing: see the module docstring."""

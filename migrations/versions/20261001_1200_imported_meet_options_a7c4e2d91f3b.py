"""Remove the `google_meet` conferencing options nobody chose.

**Every `google_meet` option before this revision was imported, not chosen.**
Nothing let a mentor pick Meet: the legacy app offered only *EduFurther video*,
*External video tool* or nothing; session-type writes never took a venue (#109);
and `mentor_conferencing_options` had no write surface until `/me/conferencing`
(#224), which ships with this revision. The rows came from three sources: a blank
legacy venue ("never chose"), an *External video tool* that could not be carried
without a URL (the quarantine), and a mentor with no legacy record. In none of
them did the mentor choose Meet. Yet they read `is_default_choice: false` and
held those mentors on Meet instead of the platform default, EduFurther video.

**So every `google_meet` row goes, with the offering pointers to it.** Those
offerings and mentors then follow the platform default. A genuine `daily` choice
is untouched. The load no longer creates them (the transform maps those three
sources to "no venue").

**Runs before the new code serves requests**, as every migration here does, so
no `google_meet` row chosen through `/me/conferencing` can exist yet.

**Downgrade is the old load rule, not a restore.** It gives each mentor with a
live offering and no default the `google_meet` default the old load would have,
and points that mentor's unpointed offerings at it. A mentor who kept a default
(a real `daily` choice alongside an imported Meet) is left alone: which imported
option the old seeding made default is not recoverable, and their offering
follows the default they kept. The dropped rows themselves are not recovered.

Revision ID: a7c4e2d91f3b
Revises: f3a91d2c7b45
Create Date: 2026-10-01 12:00:00
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c4e2d91f3b"
down_revision: str | Sequence[str] | None = "f3a91d2c7b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: First, so the `RESTRICT` composite key from `session_types` does not refuse
#: the delete. Deleted offerings too: the pointer is on the row either way.
CLEAR_POINTERS = """
UPDATE session_types st
   SET conferencing_option_id = NULL
  FROM mentor_conferencing_options o
 WHERE o.id = st.conferencing_option_id
   AND o.user_id = st.mentor_user_id
   AND o.provider = 'google_meet'
"""

DELETE_IMPORTED = """
DELETE FROM mentor_conferencing_options WHERE provider = 'google_meet'
"""

#: The old load rule: a mentor with a live offering and no default got a
#: `google_meet` default.
RESTORE_DEFAULTS = """
INSERT INTO mentor_conferencing_options (user_id, provider, is_default)
SELECT DISTINCT st.mentor_user_id, 'google_meet', true
  FROM session_types st
 WHERE st.deleted_at IS NULL
   AND NOT EXISTS (
       SELECT 1 FROM mentor_conferencing_options o
        WHERE o.user_id = st.mentor_user_id AND o.is_default
   )
ON CONFLICT (user_id, provider) DO UPDATE SET is_default = true
"""

#: Point the unpointed offerings of exactly the mentors just given that default.
RESTORE_POINTERS = """
UPDATE session_types st
   SET conferencing_option_id = o.id
  FROM mentor_conferencing_options o
 WHERE o.user_id = st.mentor_user_id
   AND o.provider = 'google_meet'
   AND o.is_default
   AND st.conferencing_option_id IS NULL
   AND st.deleted_at IS NULL
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    op.execute(CLEAR_POINTERS)
    op.execute(DELETE_IMPORTED)


def downgrade() -> None:
    """The old load rule, not a restore. See the module docstring."""
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    op.execute(RESTORE_DEFAULTS)
    op.execute(RESTORE_POINTERS)

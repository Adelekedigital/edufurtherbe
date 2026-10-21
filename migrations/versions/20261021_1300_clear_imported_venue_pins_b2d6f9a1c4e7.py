"""Clear every offering's venue pointer, so the mentor's default governs.

**Every pointer is the legacy load's.** No endpoint writes
`session_types.conferencing_option_id`: a new offering leaves it null, meaning
*use my default*, and a per-offering choice is still future work (#202). The
load alone pointed each migrated offering at the option it created for the
mentor — always also their default, so the two resolved alike until the mentor
switched default through `/me/conferencing`. Then the pinned offering kept the
old venue, and no screen shows a per-offering venue, so nothing told them
(owner, 2026-10-21). On dev: 3 of 82 offerings, all `daily`, none diverged yet.

**Only pointers move, and no venue does.** Option rows, their `source` and every
existing default stay. A mentor with no default whose live offering was pinned
first gets that option as their default, so the cleared offering resolves where
it did. Deleted offerings are cleared too, since the pointer is on the row
either way. The load stops writing them in the same
release, so a re-run or the cutover load cannot bring them back.

**Downgrade is the old load rule, not a restore**, as in `a7c4e2d91f3b`: each
live offering whose mentor's default is still `import` points at it again. A
default the mentor re-chose (`source = 'mentor'`) and a deleted offering stay
null; neither was recorded, and both resolve to the same venue either way.

Revision ID: b2d6f9a1c4e7
Revises: f4a7c2d9b318
Create Date: 2026-10-21 13:00:00
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d6f9a1c4e7"
down_revision: str | Sequence[str] | None = "f4a7c2d9b318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: First, so clearing moves nobody's venue: a mentor with **no** default whose
#: live offering is pinned gets that option as their default, where the cleared
#: offering would otherwise fall to the platform default. One per mentor (their
#: oldest pinned offering's), as the one-default index allows.
PROMOTE_ORPHAN_PINS = """
UPDATE mentor_conferencing_options o
   SET is_default = true
  FROM (
      SELECT DISTINCT ON (st.mentor_user_id) st.conferencing_option_id AS option_id
        FROM session_types st
       WHERE st.conferencing_option_id IS NOT NULL
         AND st.deleted_at IS NULL
         AND NOT EXISTS (
             SELECT 1 FROM mentor_conferencing_options d
              WHERE d.user_id = st.mentor_user_id AND d.is_default
         )
       ORDER BY st.mentor_user_id, st.created_at, st.id
  ) pinned
 WHERE o.id = pinned.option_id
"""

CLEAR_POINTERS = """
UPDATE session_types
   SET conferencing_option_id = NULL
 WHERE conferencing_option_id IS NOT NULL
"""

#: The old load rule: a live offering points at its mentor's imported default.
RESTORE_POINTERS = """
UPDATE session_types st
   SET conferencing_option_id = o.id
  FROM mentor_conferencing_options o
 WHERE o.user_id = st.mentor_user_id
   AND o.is_default
   AND o.source = 'import'
   AND st.conferencing_option_id IS NULL
   AND st.deleted_at IS NULL
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    op.execute(PROMOTE_ORPHAN_PINS)
    op.execute(CLEAR_POINTERS)


def downgrade() -> None:
    """The old load rule, not a restore. See the module docstring."""
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    op.execute(RESTORE_POINTERS)

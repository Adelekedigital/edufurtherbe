"""A self-paused mentor's return date, and which of its reminders is next.

Calendar request item 1, 2026-10-01. **Expand only**: two nullable columns on
`mentor_profiles`, metadata-only on PostgreSQL 11+, with no rewrite and nothing
for old code to read or write. On the profile rather than on the pause event,
because the event log is append-only and a return date is the *current* pause's
mutable state: changing it must not append an event.

**`apply_mentor_status` also clears both on a `listed` event**, so the event
projection stays the single writer of a mentor's listing state: a resume, an
admin relisting, an approval and an event inserted directly all end the pause
alike (#226).

**Downgrade** restores the previous function body, then drops both columns. A
paused mentor stays paused and loses only the date and whether it was reminded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c41e9a2d58"
down_revision: str | Sequence[str] | None = "a7c4e2d91f3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mentor_profiles"

#: The projection, with a `listed` event also ending the pause's return date.
APPLY_MENTOR_STATUS = """
CREATE OR REPLACE FUNCTION apply_mentor_status() RETURNS trigger AS $$
BEGIN
    IF NEW.status_type IN ('approved', 'declined') THEN
        UPDATE mentor_profiles
           SET approval_status = NEW.status_type
         WHERE user_id = NEW.mentor_user_id;
    ELSIF NEW.status_type = 'listed' THEN
        -- Any listing ends a pause, so its return date and reminder go too.
        UPDATE mentor_profiles
           SET listing_status = NEW.status_type,
               return_on = NULL,
               return_reminder_stage = NULL
         WHERE user_id = NEW.mentor_user_id;
    ELSE
        UPDATE mentor_profiles
           SET listing_status = NEW.status_type
         WHERE user_id = NEW.mentor_user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: The body `f5c3a81e6b29` installed, restored by `downgrade`.
APPLY_MENTOR_STATUS_PREVIOUS = """
CREATE OR REPLACE FUNCTION apply_mentor_status() RETURNS trigger AS $$
BEGIN
    IF NEW.status_type IN ('approved', 'declined') THEN
        UPDATE mentor_profiles
           SET approval_status = NEW.status_type
         WHERE user_id = NEW.mentor_user_id;
    ELSE
        UPDATE mentor_profiles
           SET listing_status = NEW.status_type
         WHERE user_id = NEW.mentor_user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column("return_on", sa.Date(), nullable=True))
    op.add_column(TABLE, sa.Column("return_reminder_stage", sa.SmallInteger(), nullable=True))
    op.execute(APPLY_MENTOR_STATUS)


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(APPLY_MENTOR_STATUS_PREVIOUS)
    op.drop_column(TABLE, "return_reminder_stage")
    op.drop_column(TABLE, "return_on")

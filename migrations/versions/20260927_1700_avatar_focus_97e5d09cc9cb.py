"""``user_profiles.avatar_focus_*`` — where a card should centre the avatar.

Three nullable columns, added to a live table: metadata-only in PostgreSQL, no
rewrite, no backfill in the migration (a script does that, reading the images).

- ``avatar_focus_x`` / ``avatar_focus_y``: the main face's centre, as fractions
  of the stored image, each within 0..1. Both or neither. ``numeric(4,3)``, not
  a float: the point is rounded to a thousandth, and a 32-bit float would hand
  clients ``0.5199999809`` for ``0.52``.
- ``avatar_focus_source``: ``detected`` or ``chosen``. A mentor's own choice
  outranks a detection and is never overwritten by one. ``detected`` with no
  coordinates means "looked, found no face" — so a backfill does not look again.

**`trg_clear_stale_avatar_focus`** clears the point whenever `avatar_url`
changes and the writer did not also set the focus. The upload path sets both;
the Bubble ETL and the asset re-host write `avatar_url` alone, and a point
found in the old picture says nothing about the new one. One trigger, so a
writer added later obeys it too.

Additive; old code never reads the columns, so both versions serve during the
deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "97e5d09cc9cb"
down_revision: str | Sequence[str] | None = "3c8f186fa3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_profiles"

CLEAR_FUNCTION = """
CREATE FUNCTION clear_stale_avatar_focus() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.avatar_url IS DISTINCT FROM OLD.avatar_url
       AND NEW.avatar_focus_x IS NOT DISTINCT FROM OLD.avatar_focus_x
       AND NEW.avatar_focus_y IS NOT DISTINCT FROM OLD.avatar_focus_y
       AND NEW.avatar_focus_source IS NOT DISTINCT FROM OLD.avatar_focus_source THEN
        NEW.avatar_focus_x := NULL;
        NEW.avatar_focus_y := NULL;
        NEW.avatar_focus_source := NULL;
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column("avatar_focus_x", sa.Numeric(4, 3), nullable=True))
    op.add_column(TABLE, sa.Column("avatar_focus_y", sa.Numeric(4, 3), nullable=True))
    op.add_column(TABLE, sa.Column("avatar_focus_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_user_profiles_avatar_focus_in_range"),
        TABLE,
        "(avatar_focus_x IS NULL OR avatar_focus_x BETWEEN 0 AND 1) "
        "AND (avatar_focus_y IS NULL OR avatar_focus_y BETWEEN 0 AND 1)",
    )
    op.create_check_constraint(
        op.f("ck_user_profiles_avatar_focus_both_or_neither"),
        TABLE,
        "(avatar_focus_x IS NULL) = (avatar_focus_y IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_user_profiles_avatar_focus_source_is_known"),
        TABLE,
        "avatar_focus_source IS NULL OR avatar_focus_source IN ('detected', 'chosen')",
    )
    # A point with no provenance would be read as neither, and never refreshed.
    op.create_check_constraint(
        op.f("ck_user_profiles_avatar_focus_has_a_source"),
        TABLE,
        "avatar_focus_x IS NULL OR avatar_focus_source IS NOT NULL",
    )
    op.execute(CLEAR_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_clear_stale_avatar_focus BEFORE UPDATE OF avatar_url "
        "ON user_profiles FOR EACH ROW EXECUTE FUNCTION clear_stale_avatar_focus()"
    )


def downgrade() -> None:
    """Drop the columns. Detected points are recomputable by the backfill; a
    mentor-chosen one is not, so a downgrade after that feature ships loses it."""
    op.execute("SET lock_timeout = '3s'")
    op.execute("DROP TRIGGER IF EXISTS trg_clear_stale_avatar_focus ON user_profiles")
    op.execute("DROP FUNCTION IF EXISTS clear_stale_avatar_focus()")
    for name in (
        "ck_user_profiles_avatar_focus_has_a_source",
        "ck_user_profiles_avatar_focus_source_is_known",
        "ck_user_profiles_avatar_focus_both_or_neither",
        "ck_user_profiles_avatar_focus_in_range",
    ):
        op.drop_constraint(op.f(name), TABLE, type_="check")
    op.drop_column(TABLE, "avatar_focus_source")
    op.drop_column(TABLE, "avatar_focus_y")
    op.drop_column(TABLE, "avatar_focus_x")

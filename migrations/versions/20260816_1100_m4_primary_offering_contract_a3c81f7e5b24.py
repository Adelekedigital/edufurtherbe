"""D88's contract step: the three columns leave ``mentor_profiles``.

The last of three releases. ``requires_booking_confirmation`` and
``default_meeting_venue`` now live on ``session_type_booking_configs`` and every
reader moved there in the previous release; this removes the originals.
``custom_meeting_url`` is **removed rather than moved** — nothing in ``src/``
has ever written it and nothing reads it, so relocating a column no code touches
would carry dead weight into a new table.

**The CHECK is dropped explicitly, and that is deliberate redundancy.**
``ck_mentor_profiles_custom_url_requires_custom_venue`` names two of the three
columns, and the first draft of this docstring claimed dropping them while it
existed would fail. Measured, that is false: PostgreSQL removes a CHECK along
with any column it references, so the constraint would go either way.

It stays named because the ``downgrade`` re-creates it by name, and a migration
whose two halves are asymmetric is harder to read than one line of redundancy;
and because a constraint disappearing as *collateral* of a column drop is not
the same as a constraint being *removed on purpose*, which is what happened.

Its name was read from the live database rather than from the model, because
``test_schema_parity`` exists partly for the case where those differ — its
docstring records that a constraint landing under a different name is *"silent
until a later migration drops a constraint by the name the model reports and
fails in every environment"*. They agree here.

``meeting_provider`` is **not** dropped. ``session_type_booking_configs.meeting_venue``
still uses it, and a migration that tidied away the type would take the live
column with it.

Two consequences worth stating where the next reader will find them:

* ``MeetingProvider.CUSTOM`` survives as a value with nowhere to keep its URL.
  One migrated offering is on it. Booking has to decide whether a custom venue
  needs a link, and the type cannot lose the value without the ``text`` + CHECK
  conversion first — PostgreSQL has no ``ALTER TYPE ... DROP VALUE``.
* The public leak tests guarding ``custom_meeting_url`` are deleted with the
  column. They asserted a static room link never reaches a public response; with
  no column there is nothing to leak, and a test that cannot fail is worse than
  no test because it reads as cover. If booking reintroduces a custom URL, that
  guard comes back with it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3c81f7e5b24"
down_revision = "b7f2e4c19a05"
branch_labels = None
depends_on = None

#: **Bare, and that is not a style choice.** `op.drop_constraint` runs the name
#: through the same naming convention that built it, so passing the rendered
#: `ck_mentor_profiles_custom_url_requires_custom_venue` produced
#: `ALTER TABLE mentor_profiles DROP CONSTRAINT
#:  ck_mentor_profiles_ck_mentor_profiles_custom_url_requir_5c5b` —
#: double-prefixed and truncated to 63 characters. The migration failed loudly,
#: which is the good case; the docstring above records why a *silent* version of
#: this class of mistake is what `test_schema_parity` exists for.
CHECK_NAME = "custom_url_requires_custom_venue"


def upgrade() -> None:
    op.drop_constraint(CHECK_NAME, "mentor_profiles", type_="check")
    op.drop_column("mentor_profiles", "custom_meeting_url")
    op.drop_column("mentor_profiles", "default_meeting_venue")
    op.drop_column("mentor_profiles", "requires_booking_confirmation")


def downgrade() -> None:
    """Restores the shape and **cannot restore the values.**

    Every mentor's venue and confirmation setting lives on their offerings after
    this, and there is no mentor-level answer to reconstruct: two offerings may
    legitimately disagree, which is the point of the move. A downgrade therefore
    lands every mentor on the column defaults, and anything re-reading these
    columns afterwards would be reading invented data rather than restored data.

    ``custom_meeting_url`` returns null, which is what it was on every row.
    """
    op.add_column(
        "mentor_profiles",
        sa.Column(
            "requires_booking_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "mentor_profiles",
        sa.Column(
            "default_meeting_venue",
            sa.Enum(name="meeting_provider", create_type=False),
            nullable=False,
            server_default=sa.text("'google_meet'"),
        ),
    )
    op.add_column("mentor_profiles", sa.Column("custom_meeting_url", sa.Text(), nullable=True))
    op.create_check_constraint(
        CHECK_NAME,
        "mentor_profiles",
        "custom_meeting_url IS NULL OR default_meeting_venue = 'custom'",
    )

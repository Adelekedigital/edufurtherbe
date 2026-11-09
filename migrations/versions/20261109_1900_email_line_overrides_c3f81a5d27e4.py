"""``email_line_overrides`` and ``email_line_edits``: admins' email wording (decision 318).

PRD 28. A shared Loops template's subject, heading and button come from code
defaults (`domain.email_lines.LINES`, decision 317). An override row replaces
one line until the line is saved back to its default, which deletes the row;
every save also writes one history row per changed line.

``email_line_overrides``
- ``(email_key, variant, line)`` is ``UNIQUE``: one override per line. The PRD
  drew it as the key; rule 10 keeps a ``uuid`` id and re-declares it here.
- ``email_key`` is the `Notification` value and ``variant`` ``default``, a side
  or a carried case. The database bounds their length; the admin endpoints
  check both, and the text's ``{name}``s, against the code.
- ``override_text`` and ``default_at_edit`` (the default when it was saved, to
  spot a changed one) are 1 to their line's hard max: 100, 90, 32.
- ``version`` starts at 1 and rises on every save, so a second admin's stale
  save is refused rather than overwriting.
- ``updated_by`` restricts, as ``preview_events`` does; the account purge
  anonymises rather than deletes. Has ``updated_at``, so it gets the trigger.

``email_line_edits``: insert only, so ``created_at`` alone. ``action`` is
``edit``, ``reset`` or ``keep_edit``. ``(email_key, created_at DESC)`` serves
the list's "Last edited".

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes these tables until a later PR.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f81a5d27e4"
down_revision: str | Sequence[str] | None = "abee7fed06b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINES = "line IN ('subject', 'heading', 'button')"
KEYS = "char_length(email_key) BETWEEN 1 AND 64 AND char_length(variant) BETWEEN 1 AND 64"


def _fits_its_line(column: str) -> str:
    return (
        f"(line = 'subject' AND char_length({column}) BETWEEN 1 AND 100) OR "
        f"(line = 'heading' AND char_length({column}) BETWEEN 1 AND 90) OR "
        f"(line = 'button' AND char_length({column}) BETWEEN 1 AND 32)"
    )


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False)


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "email_line_overrides",
        _id(),
        sa.Column("email_key", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("override_text", sa.Text(), nullable=False),
        sa.Column("default_at_edit", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_line_overrides")),
        sa.UniqueConstraint(
            "email_key", "variant", "line", name=op.f("uq_email_line_overrides_email_key")
        ),
        sa.CheckConstraint(LINES, name=op.f("ck_email_line_overrides_line_known")),
        sa.CheckConstraint(
            _fits_its_line("override_text"),
            name=op.f("ck_email_line_overrides_override_text_length"),
        ),
        sa.CheckConstraint(
            _fits_its_line("default_at_edit"),
            name=op.f("ck_email_line_overrides_default_at_edit_length"),
        ),
        sa.CheckConstraint(KEYS, name=op.f("ck_email_line_overrides_key_length")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_email_line_overrides_version_positive")),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_email_line_overrides_updated_by_users"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_email_line_overrides_updated_by", "email_line_overrides", ["updated_by"])
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON email_line_overrides "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        "email_line_edits",
        _id(),
        sa.Column("email_key", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("before_text", sa.Text(), nullable=False),
        sa.Column("after_text", sa.Text(), nullable=False),
        sa.Column("default_text", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("admin_id", sa.Uuid(), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_line_edits")),
        sa.CheckConstraint(LINES, name=op.f("ck_email_line_edits_line_known")),
        sa.CheckConstraint(
            "action IN ('edit', 'reset', 'keep_edit')",
            name=op.f("ck_email_line_edits_action_known"),
        ),
        sa.CheckConstraint(
            _fits_its_line("before_text"), name=op.f("ck_email_line_edits_before_text_length")
        ),
        sa.CheckConstraint(
            _fits_its_line("after_text"), name=op.f("ck_email_line_edits_after_text_length")
        ),
        sa.CheckConstraint(
            _fits_its_line("default_text"), name=op.f("ck_email_line_edits_default_text_length")
        ),
        sa.CheckConstraint(KEYS, name=op.f("ck_email_line_edits_key_length")),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["users.id"],
            name=op.f("fk_email_line_edits_admin_id_users"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_email_line_edits_email_key_created_at",
        "email_line_edits",
        ["email_key", sa.text("created_at DESC")],
    )
    op.create_index("ix_email_line_edits_admin_id", "email_line_edits", ["admin_id"])


def downgrade() -> None:
    """Drop both tables. Every email falls back to its code defaults; admins'
    edits and their history are lost."""
    op.execute("SET lock_timeout = '3s'")
    op.drop_table("email_line_edits")
    op.drop_table("email_line_overrides")

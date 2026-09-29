"""``intake_files`` — files a mentee uploads to answer a ``file_upload`` question.

A file is uploaded first and linked to a booking later, in the booking's own
transaction; ``intake_answers.file_storage_key`` gains a foreign key to
``intake_files.storage_key`` so an answer and its file cannot disagree about
which object that is.

Additive. Nothing writes ``intake_answers.file_storage_key`` before this release
(file answers were refused), so the new foreign key validates over no rows and
both code versions serve during the deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ebf9319030f7"
down_revision: str | Sequence[str] | None = "d5c69f3e05ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "intake_files"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("uploader_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("purged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_files")),
        sa.ForeignKeyConstraint(
            ["uploader_id"],
            ["users.id"],
            name=op.f("fk_intake_files_uploader_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_intake_files_session_id_sessions"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("storage_key", name=op.f("uq_intake_files_storage_key")),
        sa.CheckConstraint(
            f"content_type IN ('{PDF}', '{DOCX}')",
            name=op.f("ck_intake_files_content_type_is_known"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_intake_files_size_is_positive")),
        sa.CheckConstraint(
            "char_length(filename) BETWEEN 1 AND 255",
            name=op.f("ck_intake_files_filename_length"),
        ),
        sa.CheckConstraint(
            "purged_at IS NULL OR deleted_at IS NOT NULL",
            name=op.f("ck_intake_files_purged_after_deleted"),
        ),
    )
    op.create_index("ix_intake_files_uploader", TABLE, ["uploader_id"])
    op.create_index("ix_intake_files_session", TABLE, ["session_id"])
    op.create_index(
        "ix_intake_files_live_created",
        TABLE,
        ["created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.create_foreign_key(
        "fk_intake_answers_file_storage_key",
        "intake_answers",
        TABLE,
        ["file_storage_key"],
        ["storage_key"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Drop the table. **Refuses while it holds any row**: each is the only
    record of an object in the private bucket, and dropping it would leave those
    objects there with nothing to find, serve or expire them by."""
    op.execute("SET lock_timeout = '3s'")
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM intake_files)")).scalar():
        raise RuntimeError(
            f"{TABLE} is not empty: remove the uploaded objects and their rows first"
        )
    op.drop_constraint("fk_intake_answers_file_storage_key", "intake_answers", type_="foreignkey")
    op.drop_index("ix_intake_files_live_created", table_name=TABLE)
    op.drop_index("ix_intake_files_session", table_name=TABLE)
    op.drop_index("ix_intake_files_uploader", table_name=TABLE)
    op.drop_table(TABLE)

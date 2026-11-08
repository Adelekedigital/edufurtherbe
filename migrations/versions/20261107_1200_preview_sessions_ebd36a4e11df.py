"""``preview_sessions`` and ``preview_events``: an admin's read-only preview of
the app as one user, and its log (decision 309).

- ``preview_sessions`` belongs to an admin **and their sign-in**
  (``auth_session_id``, the access token's ``session_id`` claim), with at most
  one live preview per sign-in (``uq_preview_sessions_live``, partial on
  ``ended_at IS NULL``). Ended is ``ended_at`` and ``end_reason`` together;
  an admin never previews themselves. ``revealed_at`` is a reveal for the
  current user, cleared by a switch. Has ``updated_at``, so it gets the trigger
  (#23).
- ``preview_events`` is append-only (``created_at`` alone, no trigger). Each
  kind carries its own detail columns and only those, one ``CHECK`` per kind.
  ``ix_preview_events_session_created`` reads one preview's log in order.

Both are audit records: every user key and the session key restrict, and an
account purge anonymises the user row and keeps its id (decision 277), so
nothing deletes under them. The bounds are ``domain.preview``'s, pinned by a
test. Vocabularies are text + CHECK (#100).

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes these tables until the next PR. Creating them takes a
brief lock on ``users`` to add the keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ebd36a4e11df"
down_revision: str | Sequence[str] | None = "8c4e1f7a2d93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Children first, so the downgrade drops in order.
TABLES = ("preview_events", "preview_sessions")
END_REASONS = "('exit', 'idle', 'max_length', 'sign_out')"
#: Whether a table holds any row; the names come from `TABLES` above.
ANY_ROW = "SELECT EXISTS (SELECT 1 FROM {})"


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False)


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _user_key(column: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], ["users.id"], name=op.f(name), ondelete="RESTRICT")


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "preview_sessions",
        _id(),
        sa.Column("admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("auth_session_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        _now("started_at"),
        _now("target_since"),
        _now("last_active_at"),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("revealed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preview_sessions")),
        sa.CheckConstraint(
            f"end_reason IN {END_REASONS}", name=op.f("ck_preview_sessions_end_reason_is_known")
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (end_reason IS NULL)",
            name=op.f("ck_preview_sessions_ended_with_reason"),
        ),
        sa.CheckConstraint(
            "admin_user_id <> target_user_id", name=op.f("ck_preview_sessions_not_self")
        ),
        _user_key("admin_user_id", "fk_preview_sessions_admin_user_id_users"),
        _user_key("target_user_id", "fk_preview_sessions_target_user_id_users"),
    )
    op.create_index(
        "uq_preview_sessions_live",
        "preview_sessions",
        ["admin_user_id", "auth_session_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON preview_sessions "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        "preview_events",
        _id(),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        _now("created_at"),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("field_type", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preview_events")),
        sa.CheckConstraint(
            "kind IN ('start', 'switch', 'page_view', 'blocked_write', 'reveal', 'end')",
            name=op.f("ck_preview_events_kind_is_known"),
        ),
        sa.CheckConstraint(
            "field_type IN ('messages')", name=op.f("ck_preview_events_field_type_is_known")
        ),
        sa.CheckConstraint(
            f"end_reason IN {END_REASONS}", name=op.f("ck_preview_events_end_reason_is_known")
        ),
        sa.CheckConstraint(
            "char_length(path) BETWEEN 1 AND 2048", name=op.f("ck_preview_events_path_length")
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500", name=op.f("ck_preview_events_reason_length")
        ),
        sa.CheckConstraint(
            "(kind = 'page_view') = (path IS NOT NULL)",
            name=op.f("ck_preview_events_page_view_detail"),
        ),
        sa.CheckConstraint(
            "CASE WHEN kind = 'blocked_write' THEN method IS NOT NULL AND endpoint IS NOT NULL "
            "ELSE method IS NULL AND endpoint IS NULL END",
            name=op.f("ck_preview_events_blocked_write_detail"),
        ),
        sa.CheckConstraint(
            "CASE WHEN kind = 'reveal' THEN field_type IS NOT NULL AND reason IS NOT NULL "
            "ELSE field_type IS NULL AND reason IS NULL END",
            name=op.f("ck_preview_events_reveal_detail"),
        ),
        sa.CheckConstraint(
            "(kind = 'end') = (end_reason IS NOT NULL)",
            name=op.f("ck_preview_events_end_detail"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["preview_sessions.id"],
            name=op.f("fk_preview_events_session_id_preview_sessions"),
            ondelete="RESTRICT",
        ),
        _user_key("admin_user_id", "fk_preview_events_admin_user_id_users"),
        _user_key("target_user_id", "fk_preview_events_target_user_id_users"),
    )
    op.create_index(
        "ix_preview_events_session_created", "preview_events", ["session_id", "created_at"]
    )


def downgrade() -> None:
    """Drop both tables. **Refuses while either holds a row**: they are the only
    record of what an admin saw as someone else, so neither is dropped by
    accident."""
    op.execute("SET lock_timeout = '3s'")
    bind = op.get_bind()
    for table in TABLES:
        if bind.execute(sa.text(ANY_ROW.format(table))).scalar():
            raise RuntimeError(f"{table} is not empty: copy its rows out before downgrading")
    for table in TABLES:
        op.drop_table(table)

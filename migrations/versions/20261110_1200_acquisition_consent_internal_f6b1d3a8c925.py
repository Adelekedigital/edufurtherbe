"""Acquisition, entry point, analytics consent and internal accounts (PRD 30 PR 2).

- ``users.is_internal`` (``NOT NULL DEFAULT false``): left out of every analytics
  figure. **A projection kept by the database**, so every writer of a grant
  (the provisioning CLI, the Bubble loader, a revoke typed by hand) and every
  demo account is covered: ``internal_account(id, email)`` is a live admin grant
  or a demo address, ``trg_mark_internal_user`` sets it on insert and on any
  change of ``email`` or ``is_internal``, and ``trg_mark_internal_grantee``
  re-marks the grantee on any change to ``admin_users``, touching the row only
  when the answer changes. The demo pattern mirrors ``demo_seed.DEMO_EMAIL_LIKE``
  (no migration imports ``app``; a test pins the two together).
- ``users.signup_source`` (``NOT NULL DEFAULT 'direct'``), ``signup_channel``
  and ``auth_method`` (nullable), each a ``CHECK``; ``acquired_at`` makes a
  visitor's acquisition write-once.
- ``sessions.entry_point`` (nullable, ``CHECK``; no ``messages`` yet).
- ``analytics_consents``: one answer per person, a decline included.

**The backfills, with ``trg_set_updated_at`` held off** so ``updated_at`` keeps
Bubble's Modified Date (the migration role owns ``users``): existing admins and
demo accounts become internal, every migrated account
(``legacy_bubble_id IS NOT NULL``, the marker the Growth tab reads) becomes
``migrated``, and every existing account's acquisition is settled
(``acquired_at = created_at``), so one made long ago is never attributed to
the campaign it next signs in from. Expand only: running code reads none of it.

The functions pin ``search_path`` (Supabase's ``function_search_path_mutable``).

Downgrade drops all of it; the backfilled values are derived, so nothing is lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6b1d3a8c925"
down_revision: str | Sequence[str] | None = "e2a9c4f7b153"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INTERNAL_ACCOUNT = """
CREATE FUNCTION internal_account(account uuid, address text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = public AS $$
  SELECT address LIKE '%@demo.edufurther.test'
      OR EXISTS (SELECT 1 FROM admin_users g WHERE g.user_id = account AND g.revoked_at IS NULL)
$$;
"""

MARK_INTERNAL_USER = """
CREATE FUNCTION mark_internal_user() RETURNS trigger LANGUAGE plpgsql
SET search_path = public AS $$
BEGIN
  NEW.is_internal := internal_account(NEW.id, NEW.email);
  RETURN NEW;
END
$$;
"""

# `IS DISTINCT FROM` so a grant that changes nothing writes nothing, and the
# grantee's `updated_at` moves only when they become or stop being internal.
MARK_INTERNAL_GRANTEE = """
CREATE FUNCTION mark_internal_grantee() RETURNS trigger LANGUAGE plpgsql
SET search_path = public AS $$
DECLARE
  account uuid;
BEGIN
  FOR account IN
    SELECT DISTINCT a FROM unnest(ARRAY[
      CASE WHEN TG_OP <> 'DELETE' THEN NEW.user_id END,
      CASE WHEN TG_OP <> 'INSERT' THEN OLD.user_id END
    ]) AS a WHERE a IS NOT NULL
  LOOP
    UPDATE users SET is_internal = internal_account(id, email)
    WHERE id = account AND is_internal IS DISTINCT FROM internal_account(id, email);
  END LOOP;
  RETURN NULL;
END
$$;
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(
        "users",
        sa.Column("is_internal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("signup_source", sa.Text(), server_default=sa.text("'direct'"), nullable=False),
    )
    op.add_column("users", sa.Column("signup_channel", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("acquired_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("users", sa.Column("auth_method", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_signup_source_is_known"),
        "users",
        "signup_source IN ('invite', 'campaign', 'referral_site', 'direct', 'migrated')",
    )
    op.create_check_constraint(
        op.f("ck_users_signup_channel_is_known"),
        "users",
        "signup_channel IS NULL OR signup_channel IN "
        "('linkedin', 'google', 'whatsapp', 'instagram', 'newsletter', 'other')",
    )
    op.create_check_constraint(
        op.f("ck_users_auth_method_is_known"),
        "users",
        "auth_method IS NULL OR auth_method IN ('email_code', 'google_oauth')",
    )
    op.add_column("sessions", sa.Column("entry_point", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_sessions_entry_point_is_known"),
        "sessions",
        "entry_point IS NULL OR entry_point IN "
        "('mentor_profile', 'explore', 'home', 'onboarding_guest', 'rebook_after_review')",
    )

    op.create_table(
        "analytics_consents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_consents")),
        sa.UniqueConstraint("user_id", name=op.f("uq_analytics_consents_user_id")),
        sa.CheckConstraint(
            "char_length(version) BETWEEN 1 AND 64",
            name=op.f("ck_analytics_consents_version_length"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_analytics_consents_user_id_users"),
            ondelete="RESTRICT",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON analytics_consents "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute(INTERNAL_ACCOUNT)
    op.execute(MARK_INTERNAL_USER)
    op.execute(MARK_INTERNAL_GRANTEE)
    op.execute(
        "CREATE TRIGGER trg_mark_internal_user BEFORE INSERT OR UPDATE OF email, is_internal "
        "ON users FOR EACH ROW EXECUTE FUNCTION mark_internal_user()"
    )
    op.execute(
        "CREATE TRIGGER trg_mark_internal_grantee AFTER INSERT OR UPDATE OR DELETE "
        "ON admin_users FOR EACH ROW EXECUTE FUNCTION mark_internal_grantee()"
    )

    # The backfills. Transactional, so a failure rolls the disable back too.
    op.execute("ALTER TABLE users DISABLE TRIGGER trg_set_updated_at")
    op.execute("UPDATE users SET is_internal = true WHERE internal_account(id, email)")
    op.execute("UPDATE users SET signup_source = 'migrated' WHERE legacy_bubble_id IS NOT NULL")
    op.execute("UPDATE users SET acquired_at = created_at")
    op.execute("ALTER TABLE users ENABLE TRIGGER trg_set_updated_at")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("DROP TRIGGER trg_mark_internal_grantee ON admin_users")
    op.execute("DROP TRIGGER trg_mark_internal_user ON users")
    op.execute("DROP FUNCTION mark_internal_grantee()")
    op.execute("DROP FUNCTION mark_internal_user()")
    op.execute("DROP FUNCTION internal_account(uuid, text)")
    op.drop_table("analytics_consents")
    op.drop_constraint(op.f("ck_sessions_entry_point_is_known"), "sessions", type_="check")
    op.drop_column("sessions", "entry_point")
    for name in ("auth_method", "signup_channel", "signup_source"):
        op.drop_constraint(op.f(f"ck_users_{name}_is_known"), "users", type_="check")
    for name in ("auth_method", "acquired_at", "signup_channel", "signup_source", "is_internal"):
        op.drop_column("users", name)

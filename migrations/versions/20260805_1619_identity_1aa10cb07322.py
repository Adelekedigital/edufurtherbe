"""M1 identity — the users row and the seven tables that hang off it

Splits the legacy ``User`` table, which did five jobs in ~30 columns: identity,
profile, OAuth credentials, billing and onboarding all shared one row, so every
profile edit touched the same record as every login.

Autogenerate produced the ``create_table`` and ``create_index`` calls below and
they were reviewed line by line. **Everything else here is hand-written because
autogenerate cannot see it** — the ``citext`` extension, the five enum types,
the ``DROP TYPE`` statements, and the eight trigger attachments. Four of those
five would have failed loudly on the first upgrade; the triggers would not have
failed at all, which is why a test asserts them.

DEPARTURES FROM docs/edufurther-migration/
==========================================
ADR 0011 requires a migration that departs from the package to say so here.

**1. Five objects are absent, per ADR 0009.** ``auth_codes``, the
``auth_code_purpose`` type and ``users.password_hash`` are gone: Supabase owns
code issuance, hashing and verification, and a table we never write to is schema
asserting an implementation we do not have. ``users.id`` therefore carries **no
DEFAULT** — it *is* the Supabase auth user id, and a default would quietly mint a
valid-looking id for any insert that forgot to pass the real one, producing a row
nobody can ever log into. It is the only table in this schema whose id is not
time-ordered.

**2. Four more are deferred to the phase that first needs them**, per settled
decision #21 — ``phone_e164``/``phone_verified_at``/``phone_country_code`` (no
data, no consumer, and ADR 0009 defers phone verification), ``calendar_connections``
(M3: its DDL lives in the package's availability file, and its legacy
``composioAuthId`` values are known-dead), ``account_deletion_requests`` (no
legacy data, no feature), and the GIN full-text index on ``about_me`` (M2, with
the search that first reads it).

**3. The package's M1 table list is contradicted by the package.** Four of its
documents name four different sets — `SUMMARY.html` twice, `00_HANDOFF.md`,
`02_FIELD_MAPPING.md` and `01_identity.sql` — differing over
``calendar_connections``, ``credit_lots``, ``user_legal_consents`` and
``account_deletion_requests``. ADR 0011 makes the DDL authoritative over the
prose, so ``01_identity.sql`` wins, minus the deferrals above.

**4. ``users.slug`` is new and appears in no package document.** It is the legacy
public profile handle (``sakiratu-adeleke``), a column
`docs/bubble-data-model.md` does not list either — found only by reading the
extract. It is exactly the globally-unique human-readable key D32 warns against
while multi-tenancy is deferred, and it is carried anyway: the alternative is
discarding live profile links.

**5. ``legacy_created_at``/``legacy_modified_at`` are withdrawn, superseding the
comment in ``c707912915a9`` that promises them.** Bubble's Creation Date lands in
``created_at`` and its Modified Date in ``updated_at``. That is safe only because
the loader disables ``trg_set_updated_at`` for the duration of the import —
the trigger is unconditional by design, and an idempotent importer re-running
would otherwise stamp every migrated row with its own clock, silently, with row
counts and null-rate reconciliation both still passing. The failure log's
``set_updated_at`` row is amended accordingly.

**5b. The package's deferred foreign-key attachments are not made here.**
``01_identity.sql`` ends by attaching ``institutions.created_by`` and
``scholarship_programs.created_by``/``approved_by`` to ``users``, because in the
package those two tables are created in ``00_foundation.sql``. Under settled
decision #21 they arrive in M2, so the attachments arrive with them. Stated
because this list is only useful if it is exhaustive — a reader diffing this
migration against the spec would otherwise find an unexplained gap in the file
whose job is to explain them.

**6. ``ON DELETE`` is not uniform**, per ADR 0012. The package contradicts itself
— ``00_HANDOFF.md`` principle 4 bans cascade outright and ``01_identity.sql``
cascades every child of ``users``. Cascade where the child is meaningless without
its parent; RESTRICT on ``admin_users`` and ``user_legal_consents``, which are
audit and legal evidence. The practical effect is that ``DELETE FROM users``
fails loudly instead of taking eight tables with it.

Revision ID: 1aa10cb07322
Revises: 0ddf4037e8f2
Create Date: 2026-08-05 16:19:39.455027

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1aa10cb07322"
down_revision: str | Sequence[str] | None = "0ddf4037e8f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `users.email` is citext, so `WHERE email = :x` is case-insensitive at every
# call site rather than at the ones somebody remembered to lower(). Verified
# before adoption: reflection returns CITEXT rather than TEXT, so
# `compare_metadata` reports no diff and `alembic check` stays quiet.
#
# On Supabase, extensions are conventionally installed into an `extensions`
# schema rather than `public`. This statement does not name a schema, so it lands
# wherever the connection's search_path puts it — correct on a stock PostgreSQL
# and NOT YET VERIFIED against a real Supabase project.
CREATE_CITEXT = "CREATE EXTENSION IF NOT EXISTS citext;"

# Enum labels are the member *values* from app.domain.enums, and they are
# transcribed from schema/00_foundation.sql. Created by hand rather than
# implicitly by `create_table`, so that the matching DROP TYPE statements in
# downgrade() are visible in this file instead of merely implied — the models
# pass `create_type=False` for the same reason.
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "primary_role": ("mentee", "mentor"),
    "admin_role": ("super_admin", "mentor_approval", "limited_access"),
    "auth_provider": ("google", "linkedin"),
    "language_proficiency": ("native", "fluent", "conversational", "basic"),
    "legal_document_type": (
        "terms_of_service",
        "privacy_policy",
        "mentor_agreement",
        "community_guidelines",
    ),
}

# Reverse dependency order: every table here has a foreign key to something
# later in the list, so this is also the drop order.
TABLES: tuple[str, ...] = (
    "user_profiles",
    "user_onboarding",
    "user_languages",
    "auth_identities",
    "user_legal_consents",
    "admin_users",
    "users",
    "legal_documents",
)


def _enum(name: str) -> postgresql.ENUM:
    """Reference an enum type this migration already created.

    ``create_type=False`` matters: without it each ``create_table`` would try to
    ``CREATE TYPE`` again and the second table using a type would fail.
    """
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


def upgrade() -> None:
    """Upgrade schema.

    **Indexes are built non-concurrently, deliberately.** The `db-migration`
    standard requires `CREATE INDEX CONCURRENTLY` on a live table; every index
    here is on a table this same migration just created, so it is empty and
    invisible to any other session until commit. Building it inline is
    instantaneous, and `CONCURRENTLY` would be actively worse: it cannot run
    inside a transaction, so it would force this migration out of its
    `autocommit_block` and give up all-or-nothing application of the eight
    tables in exchange for nothing.
    """
    # The only lock contention this migration can meet is on `countries` and
    # `languages`: an inline REFERENCES takes SHARE ROW EXCLUSIVE on the
    # referenced table, which blocks writes to it (not reads) for the duration.
    #
    # That is a near-zero risk today — those tables are written only by
    # migrations — but a blocked ALTER in PostgreSQL also blocks every query
    # queued behind it, so the failure mode is a table-wide stall rather than one
    # slow statement. Failing fast and retrying is strictly better than holding
    # the queue open, and this migration will eventually run against a populated
    # production database where that distinction matters.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '60s'")

    op.execute(CREATE_CITEXT)

    for name, labels in ENUM_TYPES.items():
        rendered = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    op.create_table(
        "legal_documents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("type", _enum("legal_document_type"), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("content_url", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_legal_documents"),
    )
    op.create_index(
        "ix_legal_documents_type_version", "legal_documents", ["type", "version"], unique=True
    )

    op.create_table(
        "users",
        # No server_default. ADR 0009 §9 — this id is issued by Supabase Auth.
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("email_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column(
            "primary_role",
            _enum("primary_role"),
            server_default=sa.text("'mentee'"),
            nullable=False,
        ),
        sa.Column("timezone", sa.Text(), server_default=sa.text("'UTC'"), nullable=False),
        sa.Column("last_active_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
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
        # NOTE THE BARE NAME. The `ck` convention on Base.metadata is
        # `ck_%(table_name)s_%(constraint_name)s`, and `op.create_table` applies
        # it — so passing the already-rendered `ck_users_slug_is_url_safe` here
        # produces `ck_users_ck_users_slug_is_url_safe` in the database while the
        # model reports the single-prefixed name. `compare_metadata` does not
        # diff CHECK constraints, so nothing catches it, and the first migration
        # to `op.drop_constraint("ck_users_slug_is_url_safe", ...)` fails in every
        # environment. `pk`, `uq` and `fk` are unaffected: their templates carry
        # no `%(constraint_name)s` token, so an explicit name is used verbatim.
        sa.CheckConstraint(
            "slug IS NULL OR (slug ~ '^[a-z0-9-]+$' AND char_length(slug) BETWEEN 1 AND 60)",
            name="slug_is_url_safe",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("legacy_bubble_id", name="uq_users_legacy_bubble_id"),
    )
    # THE PARTIAL UNIQUE INDEX PEOPLE FORGET. Without `WHERE deleted_at IS NULL`
    # a soft-deleted user permanently blocks their own email from re-registering,
    # and the failure is indistinguishable from "that address is taken".
    op.create_index(
        "ix_users_email_live",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_users_slug_live",
        "users",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND slug IS NOT NULL"),
    )
    # No DESC: a single-column btree is scanned in either direction, so
    # `ORDER BY last_active_at DESC` uses this exactly as well as a descending
    # index would, without giving autogenerate an expression to compare.
    op.create_index(
        "ix_users_last_active_live",
        "users",
        ["last_active_at"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("admin_role", _enum("admin_role"), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column(
            "granted_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
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
        # All three RESTRICT (ADR 0012): this is the audit trail for elevated
        # access, and a cascade would let a hard DELETE destroy it.
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name="fk_admin_users_granted_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["users.id"],
            name="fk_admin_users_revoked_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_admin_users_user_id_users", ondelete="RESTRICT"
        ),
        # A row naming a revoker while `revoked_at` is null still matches the
        # active-grant index below — an audit trail reading "revoked by X" on a
        # grant the system considers live.
        sa.CheckConstraint(
            "revoked_by IS NULL OR revoked_at IS NOT NULL",
            name="revoker_implies_revocation",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_users"),
    )
    # One live grant per (user, role). Revoked rows stay and do not block a
    # re-grant — without the partial clause, an audit trail becomes something
    # people delete rows from to get work done.
    op.create_index(
        "ix_admin_users_active_grant",
        "admin_users",
        ["user_id", "admin_role"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "auth_identities",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", _enum("auth_provider"), nullable=False),
        sa.Column("provider_user_id", sa.Text(), nullable=False),
        sa.Column(
            "linked_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_auth_identities_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_identities"),
    )
    op.create_index(
        "ix_auth_identities_provider_user",
        "auth_identities",
        ["provider", "provider_user_id"],
        unique=True,
    )
    op.create_index("ix_auth_identities_user", "auth_identities", ["user_id"], unique=False)

    op.create_table(
        "user_languages",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("language_code", sa.CHAR(length=3), nullable=False),
        sa.Column(
            "proficiency",
            _enum("language_proficiency"),
            server_default=sa.text("'fluent'"),
            nullable=False,
        ),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["language_code"],
            ["languages.code_639_3"],
            name="fk_user_languages_language_code_languages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_languages_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "language_code", name="pk_user_languages"),
    )
    op.create_index("ix_user_languages_language", "user_languages", ["language_code"], unique=False)
    # At most one primary language per user. A plain unique on (user_id,
    # is_primary) would instead permit exactly one *non*-primary language.
    op.create_index(
        "ix_user_languages_one_primary",
        "user_languages",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )

    op.create_table(
        "user_legal_consents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("legal_document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "consented_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
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
        # Both RESTRICT (ADR 0012): this row is the answer to "prove they
        # agreed", and it is worthless if a cascade can destroy it.
        sa.ForeignKeyConstraint(
            ["legal_document_id"],
            ["legal_documents.id"],
            name="fk_user_legal_consents_legal_document_id_legal_documents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_legal_consents_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_legal_consents"),
    )
    op.create_index(
        "ix_user_legal_consents_user_document",
        "user_legal_consents",
        ["user_id", "legal_document_id"],
        unique=True,
    )

    op.create_table(
        "user_onboarding",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("last_step", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_onboarding_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_onboarding"),
    )

    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("banner_url", sa.Text(), nullable=True),
        sa.Column("about_me", sa.Text(), nullable=True),
        sa.Column("gender", sa.Text(), nullable=True),
        sa.Column("origin_country_code", sa.CHAR(length=2), nullable=True),
        sa.Column("current_country_code", sa.CHAR(length=2), nullable=True),
        sa.Column("social_linkedin", sa.Text(), nullable=True),
        sa.Column("social_twitter", sa.Text(), nullable=True),
        sa.Column("social_youtube", sa.Text(), nullable=True),
        sa.Column("email_provider_contact_id", sa.Text(), nullable=True),
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["current_country_code"],
            ["countries.code"],
            name="fk_user_profiles_current_country_code_countries",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["origin_country_code"],
            ["countries.code"],
            name="fk_user_profiles_origin_country_code_countries",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_profiles_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_profiles"),
        sa.UniqueConstraint("legacy_bubble_id", name="uq_user_profiles_legacy_bubble_id"),
    )
    op.create_index(
        "ix_user_profiles_current_country", "user_profiles", ["current_country_code"], unique=False
    )
    op.create_index(
        "ix_user_profiles_origin_country", "user_profiles", ["origin_country_code"], unique=False
    )

    # Attached per table, in the migration that creates it — settled decision
    # #23. Autogenerate is blind to triggers, and unlike the four objects above,
    # a missing one fails nothing: `updated_at` still gets a value at INSERT from
    # its server_default, so the column simply never moves again and looks
    # entirely normal. That is why a test sweeps pg_trigger rather than trusting
    # this loop.
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the eight tables in reverse dependency order, then the five enum types.

    **THIS IS DESTRUCTIVE ON A POPULATED DATABASE.** It drops every user,
    profile, linked identity, admin grant and consent record, and no downgrade
    can put them back. Reversible today only because these tables have never held
    a row outside a test.

    Once M1c has loaded, the rollback plan for this revision is **roll forward
    with a fix, plus a verified backup** — not `alembic downgrade`. The downgrade
    exists so the chain can be exercised from empty in CI, which is what proves
    the DROP TYPE statements below are right; it is not an operational recovery
    tool and should not be mistaken for one.

    **The DROP TYPE statements are the point of writing this by hand.**
    ``DROP TABLE`` removes a table's indexes, constraints and triggers, but it
    does *not* remove an enum type the table used — types are schema-level
    objects with their own lifetime. Autogenerate omits them, so the generated
    downgrade leaves five orphans behind and the *next* upgrade fails with
    ``type "primary_role" already exists``. The reversibility test catches that,
    but only after it has happened; this is the fix rather than the detection.

    Indexes are not dropped individually. ``DROP TABLE`` takes them with it, and
    listing them separately would be twenty statements whose only effect is to
    give a future editor twenty more chances to leave one behind.

    **The citext extension is deliberately not dropped**, matching how
    ``c707912915a9`` treats pgcrypto. Dropping an extension is not symmetric with
    creating it: other objects may have come to depend on it, ``DROP EXTENSION``
    then cascades or fails depending on that, and the re-upgrade is idempotent
    either way because the CREATE is ``IF NOT EXISTS``. Leaving it is the honest
    asymmetry rather than a forgotten statement.
    """
    for table in TABLES:
        op.drop_table(table)

    for name in ENUM_TYPES:
        op.execute(f"DROP TYPE {name}")

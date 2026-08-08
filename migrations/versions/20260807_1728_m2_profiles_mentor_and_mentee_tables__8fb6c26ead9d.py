"""M2 profiles - mentor and mentee tables, and usage_count removed

The seven user-owned tables of ``02_profiles.sql``, plus the removal of a column
the previous revision should not have carried.

Autogenerate produced the ``create_table`` and ``create_index`` calls and they
were reviewed line by line. Hand-written because it cannot see them: the five
``CREATE TYPE``/``DROP TYPE`` pairs, the seven trigger attachments, and the
full-text index on ``user_profiles.about_me`` — an expression index, so
``alembic check`` will not report it missing either.

DEPARTURES FROM docs/edufurther-migration/
==========================================
ADR 0011 requires a migration that departs from the package to say so here.

**1. Six tables are reshaped for ADR 0015.** The package keys ``mentor_profiles``
and ``mentee_goals`` on ``user_id`` and the three junctions on composite pairs.
Every table here carries a surrogate ``id`` instead, with the invariant the old
key carried re-declared as ``UNIQUE`` — ``user_id`` on the two 1:1 tables, the
natural pair on each junction. Losing one of those uniques would be silent:
two profiles per mentor, or a mentor listing the same offering twice.

**2. ``user_scholarship_experience`` and ``scholarship_relationship`` are not
created.** The package pairs ``user_awards`` with a second table keyed on a
``relationship ∈ awarded | applied | advised`` discriminator. The legacy field
behind it — ``Mentor Services.Scholarship Experience`` — **has no option set and
no values on any row**, so there is no vocabulary and nothing to migrate. It also
overlapped ``user_awards``: "I won Chevening" had two legal homes with nothing
choosing between them. A table with no data and no writer is schema asserting an
implementation we do not have, which is the same reasoning that removed
``password_hash`` and ``auth_codes`` in M1.

**3. ``user_awards.scholarship_program_id`` is added, and the package has no such
column.** With the table above gone, nothing anywhere in the sixty-six-table
package would reference ``scholarship_programs`` — its only external consumer was
that table. The link restores one, and takes the shape already used by
``education_entries``: ``title`` is always kept, the foreign key is optional, and
display never depends on it.

**4. ``education_entries.school_short_form`` is not created.** The package maps
legacy ``shortForm`` here and expects a school abbreviation. All 21 values in the
extract are **degree** abbreviations — BSc, Ph.D, LL.B — and on every row where
``studyProgram`` is also present the two are the same value punctuated
differently. Nothing else would write the column.

**5. ``education_entries.field_of_interest`` is not created.** Legacy
``studyFieldInsterest`` is deprecated in the source application and absent from
the export.

**6. ``requires_booking_confirmation`` defaults to ``false``**, where the package
has ``true``. Legacy stored a blank on 10 of 15 mentors and blank meant "never
turned it on", so a migrated mentor and a new one should not start on opposite
settings. The exposure is bounded: a mentor is bookable only when *approved* and
*listed*, both opted into, and in M3 a mentor with no availability has no slots
regardless.

**7. ``usage_count`` is dropped from ``institutions`` and
``scholarship_programs``.** The package declares it, indexes it, and explains it
as the admin queue's ranking signal — and specifies **nothing anywhere that
maintains it**. No trigger, no function, no application rule, so it would be zero
on every row forever and the index would sort a constant. It is also derivable,
and a stored count that drifts from what it counts is the exact defect the
package removed from ``Mentor (front search)``, whose three counters are all
dropped as "DERIVED at query time". Both pending indexes are rebuilt on
``created_at``; the ranking is computed by joining the referencing table.

**8. Country references are ``uuid`` foreign keys**, continuing the departure
argued in ``de38a0f91efd``.

DEFERRED, RECORDED SO IT IS NOT REDISCOVERED
============================================
**``unlisted_reason`` defaults to ``'never_approved'``, which is wrong for every
migrated mentor.** The unlisted mentors in the extract carry
``approvedText = Yes`` — they were approved and then turned themselves off. The
M2c transform must set this column explicitly rather than inheriting the default.

Revision ID: 8fb6c26ead9d
Revises: de38a0f91efd
Create Date: 2026-08-07 17:28:26.122009

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8fb6c26ead9d"
down_revision: str | Sequence[str] | None = "de38a0f91efd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Labels transcribed from schema/00_foundation.sql. Created by hand rather than
# implicitly by `create_table`, so the matching DROP TYPE statements are visible
# in this file instead of merely implied — the models pass `create_type=False`
# for the same reason.
#
# `scholarship_relationship` is absent and always will be; see departure 2.
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "approval_status": ("pending", "approved", "declined"),
    "listing_status": ("listed", "unlisted"),
    "unlisted_reason": ("mentor_paused", "admin_review", "dormant", "never_approved"),
    "verification_status": ("unverified", "pending", "verified", "rejected"),
    "meeting_provider": ("google_meet", "daily", "zoom", "custom"),
}

# Reverse dependency order: every table here has a foreign key to something later
# in the list, so this is also the drop order. The two goal junctions reference
# `mentee_goals(user_id)` and `mentor_service_offerings` references
# `mentor_profiles(user_id)` — not `users` — which is what makes it structurally
# impossible to attach a mentor-only row to a mentee.
TABLES: tuple[str, ...] = (
    "user_awards",
    "mentor_service_offerings",
    "mentee_goal_needs",
    "mentee_goal_countries",
    "education_entries",
    "mentor_profiles",
    "mentee_goals",
)

# Deferred from M1, which recorded it as arriving "with the search that first
# reads it". It lands here because this is where M2's schema lands; the
# alternative is one lone index inside an endpoints pull request.
#
# `coalesce` matches the package: without it a null bio yields a null tsvector
# and the row is simply absent from the index, which is defensible but differs
# from the specification for no reason.
CREATE_ABOUT_FTS = """
CREATE INDEX ix_user_profiles_about_fts ON user_profiles
  USING gin (to_tsvector('english', coalesce(about_me, '')))
"""


def _enum(name: str) -> postgresql.ENUM:
    """Reference an enum type this migration already created.

    ``create_type=False`` matters: without it each ``create_table`` would try to
    ``CREATE TYPE`` again and the second table using the type would fail.
    """
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


def upgrade() -> None:
    """Upgrade schema."""
    # Fail fast rather than queue behind a long-running transaction. These seven
    # tables are empty at creation, but the ALTERs a later phase adds will run
    # against a populated production database.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '60s'")

    for name, labels in ENUM_TYPES.items():
        rendered = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "mentee_goals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("degree_goal_id", sa.Uuid(), nullable=True),
        sa.Column("degree_goal_raw", sa.Text(), nullable=True),
        sa.Column("target_start_term", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            ["degree_goal_id"],
            ["degree_levels.id"],
            name=op.f("fk_mentee_goals_degree_goal_id_degree_levels"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_mentee_goals_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentee_goals")),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_mentee_goals_legacy_bubble_id")),
        sa.UniqueConstraint("user_id", name=op.f("uq_mentee_goals_user_id")),
    )
    op.create_table(
        "mentor_profiles",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "approval_status",
            _enum("approval_status"),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column(
            "listing_status",
            _enum("listing_status"),
            server_default=sa.text("'unlisted'"),
            nullable=False,
        ),
        sa.Column(
            "unlisted_reason",
            _enum("unlisted_reason"),
            server_default=sa.text("'never_approved'"),
            nullable=True,
        ),
        sa.Column("unlisted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("years_of_experience", sa.Integer(), nullable=True),
        sa.Column(
            "requires_booking_confirmation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "default_meeting_venue",
            _enum("meeting_provider"),
            server_default=sa.text("'google_meet'"),
            nullable=False,
        ),
        sa.Column("custom_meeting_url", sa.Text(), nullable=True),
        sa.Column("primary_study_country_id", sa.Uuid(), nullable=True),
        sa.Column("primary_study_program", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "custom_meeting_url IS NULL OR default_meeting_venue = 'custom'",
            name=op.f("ck_mentor_profiles_custom_url_requires_custom_venue"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_mentor_profiles_approved_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["primary_study_country_id"],
            ["countries.id"],
            name=op.f("fk_mentor_profiles_primary_study_country_id_countries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_mentor_profiles_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_profiles")),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_mentor_profiles_legacy_bubble_id")),
        sa.UniqueConstraint("user_id", name=op.f("uq_mentor_profiles_user_id")),
    )
    op.create_index(
        "ix_mentor_profiles_searchable",
        "mentor_profiles",
        ["approval_status", "listing_status"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_mentor_profiles_study_country",
        "mentor_profiles",
        ["primary_study_country_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_mentor_profiles_unlisted",
        "mentor_profiles",
        ["unlisted_reason", "unlisted_at"],
        unique=False,
        postgresql_where=sa.text("listing_status = 'unlisted'"),
    )
    op.create_table(
        "education_entries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("institution_id", sa.Uuid(), nullable=True),
        sa.Column("school_name_raw", sa.Text(), nullable=False),
        sa.Column("degree_level_id", sa.Uuid(), nullable=True),
        sa.Column("degree_category", sa.Text(), nullable=True),
        sa.Column("study_course", sa.Text(), nullable=True),
        sa.Column("study_program", sa.Text(), nullable=True),
        sa.Column("date_start", sa.Date(), nullable=True),
        sa.Column("date_end", sa.Date(), nullable=True),
        sa.Column("is_most_recent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.CheckConstraint(
            "date_end IS NULL OR date_start IS NULL OR date_end >= date_start",
            name=op.f("ck_education_entries_dates_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["degree_level_id"],
            ["degree_levels.id"],
            name=op.f("fk_education_entries_degree_level_id_degree_levels"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institutions.id"],
            name=op.f("fk_education_entries_institution_id_institutions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_education_entries_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_education_entries")),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_education_entries_legacy_bubble_id")),
    )
    op.create_index(
        "ix_education_entries_institution", "education_entries", ["institution_id"], unique=False
    )
    op.create_index(
        "ix_education_entries_one_most_recent",
        "education_entries",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_most_recent AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_education_entries_user",
        "education_entries",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "mentee_goal_countries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("country_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
            ["country_id"],
            ["countries.id"],
            name=op.f("fk_mentee_goal_countries_country_id_countries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mentee_goals.user_id"],
            name=op.f("fk_mentee_goal_countries_user_id_mentee_goals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentee_goal_countries")),
    )
    op.create_index(
        "ix_mentee_goal_countries_country", "mentee_goal_countries", ["country_id"], unique=False
    )
    op.create_index(
        "ix_mentee_goal_countries_pair",
        "mentee_goal_countries",
        ["user_id", "country_id"],
        unique=True,
    )
    op.create_table(
        "mentee_goal_needs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("service_offering_id", sa.Uuid(), nullable=False),
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
            ["service_offering_id"],
            ["service_offerings.id"],
            name=op.f("fk_mentee_goal_needs_service_offering_id_service_offerings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mentee_goals.user_id"],
            name=op.f("fk_mentee_goal_needs_user_id_mentee_goals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentee_goal_needs")),
    )
    op.create_index(
        "ix_mentee_goal_needs_offering", "mentee_goal_needs", ["service_offering_id"], unique=False
    )
    op.create_index(
        "ix_mentee_goal_needs_pair",
        "mentee_goal_needs",
        ["user_id", "service_offering_id"],
        unique=True,
    )
    op.create_table(
        "mentor_service_offerings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("service_offering_id", sa.Uuid(), nullable=False),
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
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_mentor_service_offerings_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_offering_id"],
            ["service_offerings.id"],
            # Shorter than the convention would render. See the model: the
            # convention name is 65 characters and PostgreSQL truncates at 63,
            # silently and with a hash.
            name=op.f("fk_mentor_service_offerings_offering"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_service_offerings")),
    )
    op.create_index(
        "ix_mentor_service_offerings_offering",
        "mentor_service_offerings",
        ["service_offering_id"],
        unique=False,
    )
    op.create_index(
        "ix_mentor_service_offerings_pair",
        "mentor_service_offerings",
        ["mentor_user_id", "service_offering_id"],
        unique=True,
    )
    op.create_table(
        "user_awards",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("institution", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("scholarship_program_id", sa.Uuid(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column(
            "verification_status",
            _enum("verification_status"),
            server_default=sa.text("'unverified'"),
            nullable=False,
        ),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "year IS NULL OR year BETWEEN 1950 AND EXTRACT(YEAR FROM now())::int + 1",
            name=op.f("ck_user_awards_year_is_sane"),
        ),
        sa.ForeignKeyConstraint(
            ["scholarship_program_id"],
            ["scholarship_programs.id"],
            name=op.f("fk_user_awards_scholarship_program_id_scholarship_programs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_awards_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["users.id"],
            name="fk_user_awards_verified_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_awards")),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_user_awards_legacy_bubble_id")),
    )
    op.create_index(
        "ix_user_awards_program", "user_awards", ["scholarship_program_id"], unique=False
    )
    op.create_index(
        "ix_user_awards_user",
        "user_awards",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(
        op.f("ix_institutions_pending"),
        table_name="institutions",
        postgresql_where="(status = 'pending_review'::lookup_status)",
    )
    op.create_index(
        "ix_institutions_pending",
        "institutions",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.drop_column("institutions", "usage_count")
    op.drop_index(
        op.f("ix_scholarship_programs_pending"),
        table_name="scholarship_programs",
        postgresql_where="(status = 'pending_review'::lookup_status)",
    )
    op.create_index(
        "ix_scholarship_programs_pending",
        "scholarship_programs",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.drop_column("scholarship_programs", "usage_count")
    # ### end Alembic commands ###

    # Attached per table, in the migration that creates it — settled decision
    # #23. Autogenerate is blind to triggers, and a missing one fails nothing:
    # `updated_at` still gets a value at INSERT from its server_default, so the
    # column simply never moves again and looks entirely normal. A test sweeps
    # pg_trigger rather than trusting this loop.
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    op.execute(CREATE_ABOUT_FTS)


def downgrade() -> None:
    """Downgrade schema.

    **The DROP TYPE statements are the point of writing this by hand.**
    ``DROP TABLE`` removes a table's indexes, constraints and triggers but leaves
    an enum type behind, so a downgrade that forgets makes the next upgrade fail
    with "type already exists". A test asserts that no enum survives a
    downgrade/upgrade cycle precisely because of that.

    ``usage_count`` is restored as ``int NOT NULL DEFAULT 0`` with its original
    index. That is honest rather than lossy: the column has only ever held zero,
    because nothing was ever written to maintain it.

    The full-text index on ``about_me`` is dropped explicitly — it belongs to
    ``user_profiles``, which this revision does not drop, so nothing else would
    remove it.
    """
    op.execute("DROP INDEX IF EXISTS ix_user_profiles_about_fts")

    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "scholarship_programs",
        sa.Column(
            "usage_count",
            sa.INTEGER(),
            server_default=sa.text("0"),
            autoincrement=False,
            nullable=False,
        ),
    )
    op.drop_index(
        "ix_scholarship_programs_pending",
        table_name="scholarship_programs",
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.create_index(
        op.f("ix_scholarship_programs_pending"),
        "scholarship_programs",
        [sa.literal_column("usage_count DESC")],
        unique=False,
        postgresql_where="(status = 'pending_review'::lookup_status)",
    )
    op.add_column(
        "institutions",
        sa.Column(
            "usage_count",
            sa.INTEGER(),
            server_default=sa.text("0"),
            autoincrement=False,
            nullable=False,
        ),
    )
    op.drop_index(
        "ix_institutions_pending",
        table_name="institutions",
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.create_index(
        op.f("ix_institutions_pending"),
        "institutions",
        [sa.literal_column("usage_count DESC")],
        unique=False,
        postgresql_where="(status = 'pending_review'::lookup_status)",
    )
    op.drop_index(
        "ix_user_awards_user",
        table_name="user_awards",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("ix_user_awards_program", table_name="user_awards")
    op.drop_table("user_awards")
    op.drop_index("ix_mentor_service_offerings_pair", table_name="mentor_service_offerings")
    op.drop_index("ix_mentor_service_offerings_offering", table_name="mentor_service_offerings")
    op.drop_table("mentor_service_offerings")
    op.drop_index("ix_mentee_goal_needs_pair", table_name="mentee_goal_needs")
    op.drop_index("ix_mentee_goal_needs_offering", table_name="mentee_goal_needs")
    op.drop_table("mentee_goal_needs")
    op.drop_index("ix_mentee_goal_countries_pair", table_name="mentee_goal_countries")
    op.drop_index("ix_mentee_goal_countries_country", table_name="mentee_goal_countries")
    op.drop_table("mentee_goal_countries")
    op.drop_index(
        "ix_education_entries_user",
        table_name="education_entries",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_education_entries_one_most_recent",
        table_name="education_entries",
        postgresql_where=sa.text("is_most_recent AND deleted_at IS NULL"),
    )
    op.drop_index("ix_education_entries_institution", table_name="education_entries")
    op.drop_table("education_entries")
    op.drop_index(
        "ix_mentor_profiles_unlisted",
        table_name="mentor_profiles",
        postgresql_where=sa.text("listing_status = 'unlisted'"),
    )
    op.drop_index(
        "ix_mentor_profiles_study_country",
        table_name="mentor_profiles",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_mentor_profiles_searchable",
        table_name="mentor_profiles",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("mentor_profiles")
    op.drop_table("mentee_goals")
    # ### end Alembic commands ###

    for name in ENUM_TYPES:
        op.execute(f"DROP TYPE {name}")

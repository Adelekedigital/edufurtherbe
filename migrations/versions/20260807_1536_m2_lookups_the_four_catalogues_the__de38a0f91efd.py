"""M2 lookups - the four catalogues the profile tables reference

Two of these are **open** — users create rows in ``institutions`` and
``scholarship_programs``, and an admin curates them, which is what ``status``,
``merged_into_id`` and ``usage_count`` are for. Two are **closed** —
``degree_levels`` and ``service_offerings`` are vocabularies the product defines
and nobody can extend. That split is the whole design, and it is visible in the
column lists rather than written down somewhere else.

Autogenerate produced the ``create_table`` and ``create_index`` calls and they
were reviewed line by line. It did **better than expected**: it rendered both GIN
trigram indexes with their ``postgresql_ops``, both partial indexes, and the
``source`` CHECK, because all three are declared on the models. What it cannot
see, and what is hand-written below, is the ``pg_trgm`` extension, the
``CREATE TYPE``/``DROP TYPE`` pair, the four trigger attachments, and the seeds.

DEPARTURES FROM docs/edufurther-migration/
==========================================
ADR 0011 requires a migration that departs from the package to say so here.

**1. Country is a foreign key to ``countries.id``, not ``char(2)``.** The package
writes ``country_code char(2) REFERENCES countries(code)`` on both open tables,
which was right when ISO lookups were keyed on their code. ADR 0015 reversed
that, so the reference stores the id like every other foreign key in the schema.

**2. No deferred foreign-key attachments.** ``01_identity.sql`` ends by
``ALTER TABLE``-ing ``institutions.created_by`` and
``scholarship_programs.created_by``/``approved_by`` onto ``users``, purely because
in the package those tables are created in ``00_foundation.sql`` — before ``users``
exists. Our chain runs the other way: ``users`` shipped in M1, so these are
ordinary inline foreign keys. There is nothing deferred and nothing to attach
later. Both ``scholarship_programs`` references to ``users`` are named by hand,
because the convention in ``base.py`` renders on ``column_0_name`` and cannot
disambiguate a second foreign key to the same table.

**3. Indexes are ``ix_``-prefixed, not ``idx_``.** The package uses ``idx_``; M1
normalised to ``ix_`` to match the naming convention, and this follows M1 rather
than reintroducing a second spelling.

**4. Only ``lookup_status`` ships.** ``02_profiles.sql`` declares seven enums this
phase will eventually need; the other six — ``approval_status``,
``listing_status``, ``unlisted_reason``, ``meeting_provider``,
``verification_status`` and ``scholarship_relationship`` — are consumed by tables
in the *next* pull request and arrive with them, per settled decision #21. A type
no table uses is a schema asserting a choice nobody took.

**5. ``service_offerings`` is seeded with six rows and the package seeds none.**
The package leaves the table empty while D12 calls it "THE SHARED VOCABULARY",
so the content had to come from somewhere. It came from the legacy option set,
and reading it corrected D12's premise: Bubble did **not** hold two separate
option sets. It held one, used by both sides — but both columns store the display
name as text at the moment of selection rather than as a live reference, so the
mentee side is six parent values and the mentor side is five parents plus five
children and renames. Seeding the six parents is what makes matching work at all:
today a mentor offering "Document Review" can never match a mentee needing
"document preparation".

**6. ``scholarship_programs`` is seeded with ten rows and the package seeds
none.** D15 makes the merge path mandatory precisely because "Chevening",
"chevening scholarship" and "Chevening Award" otherwise become three rows inside
a month — but populate-on-demand with an empty table gives suggest-before-create
nothing to suggest against. These ten are curated, so they ship approved.

**7. ``funding_type`` and ``degree_levels`` are left empty on every seeded row.**
Reliable coverage data exists for perhaps four of the ten programmes, and a
half-populated column reads as authoritative to whoever next writes a filter.
``degree_levels`` is additionally a ``text[]`` of slugs with no foreign key behind
it — a soft reference that a slug rename would silently invalidate. Nothing
writes it this phase, and whether it stays an array or becomes a junction is a
question for the code that first populates it.

DEFERRED DECISIONS, RECORDED SO THEY ARE NOT REDISCOVERED
=========================================================
**``status`` defaults to ``'approved'`` and that is fail-open.** A write path
that forgets to set it publishes a user-created row globally. The default flips
to ``'pending_review'`` in the pull request that ships admin curation; **until
then every user-facing write path must set ``'pending_review'`` explicitly.**
Written here because the safe behaviour currently depends on somebody
remembering, which is the shape this project's failure log keeps recording.

**Legacy ``meetingDuration`` is an M4 input.** ``02_profiles.sql`` says mentor
duration is "dropped", fifteen lines below the note that every migrated mentor
gets an auto-created "General Mentorship" session type — which is exactly where a
mentor's chosen 15/30/45/60 belongs. It is read from the staged snapshot in M4,
not carried through M2.

Revision ID: de38a0f91efd
Revises: e25541374c03
Create Date: 2026-08-07 15:36:58.226171

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "de38a0f91efd"
down_revision: str | Sequence[str] | None = "e25541374c03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `pg_trgm` is the first extension whose *operators* this schema depends on.
# `pgcrypto` was declared for tidiness in `c707912915a9`, but `gen_random_uuid()`
# has been core since PostgreSQL 13, so its absence would never have surfaced.
# A missing `pg_trgm` fails loudly at CREATE INDEX instead.
#
# No schema qualification: it installs into `public` locally and into Supabase's
# `extensions` schema in production, and both are on the default search path, so
# `gin_trgm_ops` resolves either way. Verified against both rather than assumed —
# the failure mode is an installed extension whose operator class the index
# cannot find.
CREATE_TRGM = "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

# Labels transcribed from schema/00_foundation.sql. Created by hand rather than
# implicitly by `create_table`, so the matching DROP TYPE in downgrade() is
# visible in this file instead of merely implied — the model passes
# `create_type=False` for the same reason.
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "lookup_status": ("approved", "pending_review", "merged", "rejected"),
}

# Drop order. Nothing in M2 references these yet — `education_entries` and the
# two junctions arrive in the next pull request — so the order is only about the
# self-references, which DROP TABLE handles regardless. Listed explicitly anyway,
# because the next phase will add rows to this tuple and an implicit order is one
# more thing to get wrong then.
TABLES: tuple[str, ...] = (
    "scholarship_programs",
    "institutions",
    "service_offerings",
    "degree_levels",
)

# --- Seeds ------------------------------------------------------------------
#
# Rows as tuples with the column names stated once. Spelling every key on every
# row generated a 561 KB migration for the reference data and would have been
# rejected by the commit-size hook; these three are small enough that it would
# not matter, which is exactly why the habit has to be the same either way.

# The package's six, verbatim.
DEGREE_LEVELS: tuple[tuple[str, str, int], ...] = (
    ("undergraduate", "Undergraduate", 10),
    ("diploma", "Diploma", 20),
    ("masters", "Masters", 30),
    ("mba", "MBA", 40),
    ("phd", "PhD", 50),
    ("postdoc", "Postdoctoral", 60),
)

# The six parent values of the legacy option set. Children — GRE/GMAT/IELTS under
# Test Preparation, SOP/Personal statement/Resume under Document Preparation, and
# so on — are deliberately absent, and **no `parent_id` column is carried "for
# later"**: ADR 0008 rejected exactly that shape, because a column nothing
# populates is indistinguishable from one somebody forgot to populate. Adding the
# hierarchy later is additive. Splitting or renaming a parent is not, since this
# table has no `merged_into_id` — so these six stay stable.
SERVICE_OFFERINGS: tuple[tuple[str, str, str, int], ...] = (
    ("test-preparation", "Test Preparation", "test", 10),
    ("document-preparation", "Document Preparation", "application", 20),
    ("school-selection", "School Selection", "application", 30),
    ("program-selection", "Program Selection", "application", 40),
    ("scholarships-financial-aid", "Scholarships & Financial Aid", "funding", 50),
    ("interview-preparation", "Interview Preparation", "application", 60),
)

# Curated, so they ship at the column default `'approved'` rather than
# `'pending_review'`. The two assistantships are the arguable pair: they are
# funding *mechanisms* every university runs rather than named schemes, so
# nothing distinguishes one institution's from another's. They are included
# because they are peers of the rest in the legacy option set, and because
# `merged_into_id` makes that reversible if it proves wrong.
#
# The third element is an ISO 3166-1 alpha-2 code resolved to `countries.id` at
# insert time, never a literal UUID: reference ids are generated per environment,
# so a literal would be correct in one database and silently wrong in every other.
SCHOLARSHIP_PROGRAMS: tuple[tuple[str, str, str | None], ...] = (
    ("mastercard-foundation", "MasterCard Foundation Scholarship Program", None),
    ("fulbright", "Fulbright Scholarship", "US"),
    ("commonwealth", "Commonwealth Scholarship", "GB"),
    ("graduate-assistantship", "Graduate Assistantship", None),
    ("chevening", "Chevening UK Government Scholarship", "GB"),
    ("daad", "DAAD - German Academic Exchange Service", "DE"),
    ("research-assistantship", "Research Assistantship", None),
    ("rhodes", "Rhodes Scholarship", "GB"),
    ("skoll", "Skoll Scholarship", "GB"),
    ("knight-hennessy", "Stanford Knight-Hennessy Scholars", "US"),
)


def _enum(name: str) -> postgresql.ENUM:
    """Reference an enum type this migration already created.

    ``create_type=False`` matters: without it each ``create_table`` would try to
    ``CREATE TYPE`` again and the second table using the type would fail.
    """
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


def _seed(bind: sa.Connection) -> None:
    """Insert the three seeded vocabularies.

    ``institutions`` gets no seed: it is populated on demand from hipolabs when a
    user selects a school (ADR 0008), so an empty table here is correct rather
    than an omission.
    """
    bind.execute(
        sa.text(
            "INSERT INTO degree_levels (slug, display_name, sort_order) "
            "VALUES (:slug, :display_name, :sort_order)"
        ),
        [
            dict(zip(("slug", "display_name", "sort_order"), row, strict=True))
            for row in DEGREE_LEVELS
        ],
    )

    bind.execute(
        sa.text(
            "INSERT INTO service_offerings (slug, display_name, category, sort_order) "
            "VALUES (:slug, :display_name, :category, :sort_order)"
        ),
        [
            dict(zip(("slug", "display_name", "category", "sort_order"), row, strict=True))
            for row in SERVICE_OFFERINGS
        ],
    )

    # A code that does not resolve makes the subquery return NULL, which would
    # land a null `country_id` and look exactly like a programme we deliberately
    # left unattributed. The assertion below is what turns that into a failure.
    bind.execute(
        sa.text(
            "INSERT INTO scholarship_programs (slug, display_name, country_id) VALUES "
            "(:slug, :display_name, (SELECT id FROM countries WHERE code = :code))"
        ),
        [
            dict(zip(("slug", "display_name", "code"), row, strict=True))
            for row in SCHOLARSHIP_PROGRAMS
        ],
    )

    expected = [slug for slug, _, code in SCHOLARSHIP_PROGRAMS if code is not None]
    unresolved = (
        bind.execute(
            sa.text(
                "SELECT slug FROM scholarship_programs "
                "WHERE slug = ANY(:slugs) AND country_id IS NULL ORDER BY slug"
            ),
            {"slugs": expected},
        )
        .scalars()
        .all()
    )
    if unresolved:
        raise RuntimeError(
            f"country code did not resolve against `countries` for: {', '.join(unresolved)}. "
            "A null country_id here is indistinguishable from a deliberate one."
        )


def upgrade() -> None:
    """Upgrade schema."""
    # Matching M1: fail fast rather than queue behind a long-running transaction.
    # These four tables are empty at creation, but the statement that seeds them
    # and the ALTERs a later phase adds will run against a populated production
    # database.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '60s'")

    op.execute(CREATE_TRGM)

    for name, labels in ENUM_TYPES.items():
        rendered = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    op.create_table(
        "degree_levels",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_degree_levels")),
        sa.UniqueConstraint("slug", name=op.f("uq_degree_levels_slug")),
    )
    op.create_table(
        "service_offerings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_offerings")),
        sa.UniqueConstraint("slug", name=op.f("uq_service_offerings_slug")),
    )
    op.create_table(
        "institutions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("country_id", sa.Uuid(), nullable=False),
        sa.Column(
            "alt_names", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("web_page", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), server_default=sa.text("'hipolabs'"), nullable=False),
        sa.Column(
            "status",
            _enum("lookup_status"),
            server_default=sa.text("'approved'"),
            nullable=False,
        ),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column("usage_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
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
            "source IN ('hipolabs', 'manual', 'ror')", name=op.f("ck_institutions_source_is_known")
        ),
        sa.ForeignKeyConstraint(
            ["country_id"],
            ["countries.id"],
            name=op.f("fk_institutions_country_id_countries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_institutions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["institutions.id"],
            name=op.f("fk_institutions_merged_into_id_institutions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_institutions")),
        sa.UniqueConstraint("domain", name=op.f("uq_institutions_domain")),
    )
    op.create_index("ix_institutions_country", "institutions", ["country_id"], unique=False)
    op.create_index(
        "ix_institutions_name_trgm",
        "institutions",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_institutions_pending",
        "institutions",
        [sa.literal_column("usage_count DESC")],
        unique=False,
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.create_table(
        "scholarship_programs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("country_id", sa.Uuid(), nullable=True),
        sa.Column("funding_type", sa.Text(), nullable=True),
        sa.Column(
            "degree_levels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("official_url", sa.Text(), nullable=True),
        sa.Column(
            "status",
            _enum("lookup_status"),
            server_default=sa.text("'approved'"),
            nullable=False,
        ),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column("usage_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
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
        # Named by hand, both of them: the convention renders on `column_0_name`
        # and would emit the same name for two foreign keys to `users`.
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_scholarship_programs_approved_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["country_id"],
            ["countries.id"],
            name=op.f("fk_scholarship_programs_country_id_countries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_scholarship_programs_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["scholarship_programs.id"],
            name=op.f("fk_scholarship_programs_merged_into_id_scholarship_programs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scholarship_programs")),
        sa.UniqueConstraint("slug", name=op.f("uq_scholarship_programs_slug")),
    )
    op.create_index(
        "ix_scholarship_programs_name_trgm",
        "scholarship_programs",
        ["display_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"display_name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_scholarship_programs_pending",
        "scholarship_programs",
        [sa.literal_column("usage_count DESC")],
        unique=False,
        postgresql_where=sa.text("status = 'pending_review'"),
    )

    # Attached per table, in the migration that creates it — settled decision
    # #23. Autogenerate is blind to triggers, and a missing one fails nothing:
    # `updated_at` still gets a value at INSERT from its server_default, so the
    # column simply never moves again and looks entirely normal. That is why a
    # test sweeps pg_trigger rather than trusting this loop.
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    _seed(op.get_bind())


def downgrade() -> None:
    """Downgrade schema.

    **The DROP TYPE statement is the point of writing this by hand.** DROP TABLE
    removes a table's indexes, constraints and triggers but leaves an enum type
    behind, so a downgrade that forgets it makes the next upgrade fail with
    "type already exists" — and the test that asserts no enum survives a
    downgrade/upgrade cycle exists because of that.

    ``pg_trgm`` is deliberately **not** dropped, for the same asymmetry
    ``c707912915a9`` records for ``pgcrypto``: other objects may have come to
    depend on an extension, DROP cascades or fails depending on that, and the
    re-upgrade is idempotent either way because the CREATE is ``IF NOT EXISTS``.

    Seeded rows go with their tables. Nothing else is lost — no user data exists
    at this revision.
    """
    for table in TABLES:
        op.drop_table(table)

    for name in ENUM_TYPES:
        op.execute(f"DROP TYPE {name}")

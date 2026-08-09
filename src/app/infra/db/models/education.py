"""Education: where someone studied, and at what level.

Two lookups and the user-owned table that references both. The module is named
for the subject rather than the layer (settled decision #33), so the question
"where does this table go" has one answer for all remaining tables instead of a
fresh judgement each time.

``institutions`` **is** a mirror (ADR 0020, superseding that part of ADR 0008).
The catalogue is fetched over HTTPS from the source repository and refreshed
weekly, 10,250 rows; the `http://` hipolabs API is never called, because a
browser on an HTTPS page cannot reach it. There is deliberately **no
``ror_id``** — ADR 0008 supersedes the settled decision that defined one, and the
absence is one of that record's stated confirmations, untouched by 0020.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, Date, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import LookupStatus
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


class Institution(TimestampMixin, Base):
    """A university someone has studied at, mirrored from hipolabs.

    **``domain`` is the natural key, not the primary key.** Names change and
    domains rarely do, so an upsert deduplicates on ``domain`` — but it is null
    for ``source='manual'``, and a nullable column cannot be a primary key. The
    surrogate ``id`` is what every other row points at (ADR 0015).

    **The country is a foreign key to ``countries.id``, not a ``char(2)``.** The
    package writes ``country_code char(2) REFERENCES countries(code)``, which was
    correct when ISO lookups were keyed on their code. ADR 0015 reversed that, so
    the reference stores the id like every other foreign key in the schema.

    ``created_by`` restricts rather than cascades, per ADR 0013: an institution is
    catalogue data, not a user-owned row, so deleting the user who first selected
    it must not take the row — and every education entry pointing at it — with
    them.
    """

    __tablename__ = "institutions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Unique but nullable, and PostgreSQL treats nulls as distinct — so any
    # number of `source='manual'` rows coexist without a domain while two
    # hipolabs rows can never share one. That is the whole deduplication story.
    domain: Mapped[str | None] = mapped_column(Text, unique=True)

    # Nullable, and only for the manual path. A mirrored row always carries a
    # country — hipolabs supplies `alpha_two_code` on every record. A row a user
    # created by typing a name we do not hold has none, and an admin supplies it
    # at review: asking the user for a field the review process exists to fill is
    # friction on the person, not on the system.
    country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="RESTRICT")
    )

    # When this row was last seen in the upstream catalogue.
    #
    # Distinct from `updated_at`, which means "this row's content changed". A
    # sync stamps every row it saw, with `trg_set_updated_at` held off, so:
    #
    #   max(last_synced_at)            -> when the catalogue was last refreshed
    #   last_synced_at < that maximum  -> upstream no longer carries this row
    #
    # Stamping only *changed* rows would leave the maximum frozen through a week
    # where the source did not move, making "checked, nothing new" and "not
    # checked in a month" identical — which is precisely the ambiguity ADR 0008
    # raised against mirroring. Null for a `source='manual'` row, which no sync
    # ever saw.
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Local aliases hipolabs does not carry. ADR 0008 names this as one of three
    # mechanisms recovering from hipolabs being incomplete for African
    # institutions — the coverage gap that record accepts without having sized.
    alt_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )

    web_page: Mapped[str | None] = mapped_column(Text)

    # Provenance, not an implementation claim. `'ror'` is permitted and nothing
    # writes it — ADR 0008 keeps it because a provenance value costs one word,
    # and removing it would foreclose the documented revisit. Text with a CHECK
    # rather than an enum, matching the package: this is a closed set, but it is
    # the package's own shape and there is no reason to depart from it.
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'hipolabs'"))

    status: Mapped[LookupStatus] = mapped_column(
        pg_enum(LookupStatus), nullable=False, server_default=text("'approved'")
    )

    # The losing row of a merge survives and points here, so a client holding a
    # cached reference still resolves. Self-referencing, and RESTRICT because
    # deleting a merge target would leave the pointer dangling.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("institutions.id", ondelete="RESTRICT")
    )

    # Null for every seeded or hipolabs-sourced row; set only when a user creates
    # one. The same honest null as `admin_users.granted_by` on a bootstrap grant.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        Index("ix_institutions_country", "country_id"),
        # The trigram index. `pg_trgm` installs into `public` locally and into
        # Supabase's `extensions` schema in production; both are on the default
        # search path, so `gin_trgm_ops` resolves without qualification —
        # verified against both, because a missing operator class fails at
        # CREATE INDEX with the extension already installed.
        Index(
            "ix_institutions_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # Case-insensitive prefix, for the first tier of autocomplete.
        #
        # **The trigram index above does not serve a prefix search**, and this is
        # measured rather than assumed: on 10,250 rows `name ILIKE 'University%'`
        # without this index is a sequential scan at 59ms, and 4.3ms with it.
        # Autocomplete runs a query per keystroke, so the prefix tier is the
        # common path, not an optimisation for a rare one.
        #
        # `text_pattern_ops` is what makes a btree usable for `LIKE 'x%'` at all —
        # the default operator class sorts by collation, and a prefix scan needs
        # character ordering. `lower(name)` rather than `name` because the search
        # is case-insensitive; the query must match this expression exactly or
        # the planner silently ignores the index.
        Index(
            "ix_institutions_name_prefix",
            text("lower(name) text_pattern_ops"),
        ),
        # The admin work queue: pending rows, oldest first.
        #
        # The package ranks this by a stored `usage_count`, which is dropped —
        # nothing anywhere maintained it, so it was zero on every row forever,
        # and the index sorted a constant. It is also derivable, and a stored
        # count that drifts from what it counts is the exact defect the package
        # removed from `Mentor (front search)`, whose three counters are all
        # dropped as "DERIVED at query time".
        #
        # The ranking the queue actually wants — eight users is an approval, one
        # user plus a typo is a merge — is now computed:
        #
        #   SELECT i.id, i.name, count(e.id) AS uses
        #   FROM institutions i
        #   LEFT JOIN education_entries e ON e.institution_id = i.id
        #   WHERE i.status = 'pending_review'
        #   GROUP BY i.id, i.name ORDER BY uses DESC;
        #
        # ADR 0008 records that this mechanism exists and its owner does not.
        Index(
            "ix_institutions_pending",
            "created_at",
            postgresql_where=text("status = 'pending_review'"),
        ),
        # Bare name; the `ck` convention renders the `ck_institutions_` prefix.
        CheckConstraint(
            "source IN ('hipolabs', 'manual', 'ror')",
            name="source_is_known",
        ),
    )


class DegreeLevel(TimestampMixin, Base):
    """Undergraduate, Masters, PhD — the closed, filterable degree dimension.

    Replaces legacy ``Members Goals.degreeGoal``, free text across 720 rows where
    "Masters", "masters", "MSc" and "Master's Degree" all coexisted and no filter
    worked. Values that cannot be mapped land in ``mentee_goals.degree_goal_raw``
    rather than being dropped.

    **This is the dimension that gets filtered on; the specific programme is
    not.** "Mentors with a PhD" runs here, against six rows. The programme itself
    — "MSc (Master of Science)", "LLB (Bachelor of Law)" — is display-only text on
    ``education_entries``, served by a frontend autocomplete rather than a lookup,
    because nothing matches or filters on it.

    Closed: no ``status``, no ``merged_into_id``, no ``created_by``. Users cannot
    add a row, so there is nothing to curate. ``is_active`` retires a value
    without breaking the rows that already point at it.
    """

    __tablename__ = "degree_levels"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # The stable identifier code refers to. Environment-generated ids cannot be
    # written into a seed, a test or a query filter, and `display_name` is free
    # to be re-worded by product — so neither can be the thing a transform keys
    # on. The demoted natural key keeps its own UNIQUE (non-negotiable #10).
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class EducationEntry(TimestampMixin, Base):
    """One degree a user holds. Legacy ``Education``, 940 rows.

    **``school_name_raw`` is always kept and ``institution_id`` is nullable**,
    and that pair is what makes an incomplete registry survivable rather than
    lossy (ADR 0008, point 5). An entry that never matches still displays what
    the user typed, and can be linked opportunistically the next time they edit
    their profile.

    **Country is never asked for.** It derives from ``institutions.country_id``,
    resolved once at write. Storing the raw string instead would mean
    re-querying hipolabs on every profile render, and "who studied in the UK"
    would be a runtime API fan-out rather than a join.

    Two package columns are deliberately absent:

    - ``school_short_form`` — the package maps legacy ``shortForm`` here and
      says it "mostly folds into ``institutions.alt_names``". The data says
      otherwise: all 21 values are **degree** abbreviations (BSc, Ph.D, LL.B),
      and on every row where ``studyProgram`` is also present the two are the
      same value punctuated differently. Folding "BSc" into a university's
      aliases would corrupt the registry. Nothing else would feed the column, so
      it does not ship.
    - ``field_of_interest`` — legacy ``studyFieldInsterest`` is deprecated in
      the source application and absent from the export entirely.

    ``degree_category`` **is** kept alongside ``degree_level_id``, and that is
    not duplication: it is the same raw-plus-resolved pair as
    ``school_name_raw`` + ``institution_id``. After cutover the raw value cannot
    be re-fetched, and the mapping is only as good as its next revision.
    """

    __tablename__ = "education_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("institutions.id", ondelete="RESTRICT")
    )
    school_name_raw: Mapped[str] = mapped_column(Text, nullable=False)

    degree_level_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("degree_levels.id", ondelete="RESTRICT")
    )
    degree_category: Mapped[str | None] = mapped_column(Text)
    study_course: Mapped[str | None] = mapped_column(Text)
    study_program: Mapped[str | None] = mapped_column(Text)

    # `date`, not `timestamptz`. The export renders these as midnight local, and
    # the transform takes the date **as rendered in America/New_York** rather
    # than converting to UTC first — every dev value is 12:00 am, which makes the
    # wrong version latent rather than visible.
    date_start: Mapped[date | None] = mapped_column(Date)
    date_end: Mapped[date | None] = mapped_column(Date)

    is_most_recent: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        Index(
            "ix_education_entries_user",
            "user_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_education_entries_institution", "institution_id"),
        # Enforces one-most-recent-per-user in the database. `latestUniversity`
        # on the old front-search table derives from this.
        #
        # The WHERE clause is the whole constraint. Without it this becomes a
        # plain unique index on `user_id` and a user may hold exactly one degree
        # — which is not a subtle degradation, but it would pass any test that
        # only ever inserts one row per user.
        Index(
            "ix_education_entries_one_most_recent",
            "user_id",
            unique=True,
            postgresql_where=text("is_most_recent AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            "date_end IS NULL OR date_start IS NULL OR date_end >= date_start",
            name="dates_ordered",
        ),
    )

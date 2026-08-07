"""Education: where someone studied, and at what level.

Two lookups now; ``education_entries`` joins them in the next pull request. The
module is named for the subject rather than the layer (settled decision #33), so
the question "where does this table go" has one answer for all 58 remaining
tables instead of a fresh judgement each time.

``institutions`` is a **registry, not a mirror** (ADR 0008). Autocomplete is
served from hipolabs and that catalogue is never cached; a row lands here only
once somebody selects it, which is ~200-400 rows drawn from 940 legacy education
entries rather than the ~9,000 a mirror would carry. There is deliberately **no
``ror_id``** — ADR 0008 supersedes the settled decision that defined one, and the
absence is one of that record's stated confirmations.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import LookupStatus
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


class Institution(TimestampMixin, Base):
    """A university someone has studied at, stored once referenced.

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

    country_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="RESTRICT"), nullable=False
    )

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

    usage_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))

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
        # The admin work queue: pending rows, most-used first. A pending entry
        # with eight users is an approval; one with a single user and a typo is a
        # merge. ADR 0008 records that this mechanism exists and its owner does
        # not.
        Index(
            "ix_institutions_pending",
            text("usage_count DESC"),
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

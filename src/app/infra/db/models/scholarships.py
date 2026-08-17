"""Scholarships: the catalogue of named funding schemes.

A **scholarship programme** is a named, externally-run funding scheme a student
applies to — Chevening, Fulbright, MasterCard Foundation. It exists
independently of any user, exactly like an institution. It is not a kind of
funding, not an amount, and not something one university invented for itself.

``user_awards`` is what links a *person* to one of those rows, and it is the
only thing that does. The package pairs it with a separate
``user_scholarship_experience`` table carrying a
``relationship ∈ awarded | applied | advised`` discriminator; **that table does
not ship**. The legacy field behind it has no option set, no values on any row,
and therefore nothing to migrate — and it overlapped ``user_awards`` besides, so
"I won Chevening" had two legal homes with nothing choosing between them.

What is genuinely lost is narrower than it looks: *applied* and *advised* have
no expression this phase. Neither has legacy data, and "I advise on Chevening"
is a mentor capability that belongs with the service hierarchy rather than here.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import LookupStatus, VerificationStatus
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class ScholarshipProgram(TimestampMixin, Base):
    """A named funding scheme, seeded with what is known and extended on demand.

    **The merge path is mandatory, not optional** (package D15). Without
    suggest-before-create and an admin merge, "Chevening", "chevening
    scholarship" and "Chevening Award" become three live rows inside a month and
    filtering by scholarship stops working. ``merged_into_id`` plus the trigram
    index below are what make that recoverable, and the signal that tells an
    approval from a typo — eight users versus one user with a near-match — is
    computed from the rows that reference a programme rather than stored.

    ``slug`` is **nullable here**, unlike ``degree_levels.slug`` — a row a user
    created has no stable identifier until somebody curates it, and inventing one
    from their typo would be worse than leaving it null.

    ``country_id`` replaces the package's ``country_code char(2)`` for the reason
    given on ``Institution``: ADR 0015 keys ISO lookups on a surrogate id.
    """

    __tablename__ = "scholarship_programs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    slug: Mapped[str | None] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

    country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="RESTRICT")
    )

    # How much is covered — full | partial | tuition_only | stipend. Left null
    # across the entire seed on purpose: reliable coverage data exists for maybe
    # four of the ten, and a half-populated column reads as authoritative to
    # whoever next writes a filter against it.
    funding_type: Mapped[str | None] = mapped_column(Text)

    # An array of degree-level slugs, per the package — a soft reference to
    # `degree_levels.slug` with no foreign key behind it, so a slug rename leaves
    # it silently stale. Nothing writes it in this phase. Whether it stays an
    # array or becomes a junction is a question for the code that first
    # populates it, and that code does not exist yet.
    degree_levels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )

    official_url: Mapped[str | None] = mapped_column(Text)

    status: Mapped[LookupStatus] = mapped_column(
        str_enum(LookupStatus), nullable=False, server_default=text("'approved'")
    )

    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("scholarship_programs.id", ondelete="RESTRICT")
    )

    # Two foreign keys to the same table, so both are named explicitly: the
    # convention in `base.py` renders on `column_0_name` and cannot disambiguate
    # a second reference to `users`. Both RESTRICT, per ADR 0013's rule for
    # actor columns.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_scholarship_programs_created_by_users"
        ),
    )
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_scholarship_programs_approved_by_users"
        ),
    )

    __table_args__ = (
        Index(
            "ix_scholarship_programs_name_trgm",
            "display_name",
            postgresql_using="gin",
            postgresql_ops={"display_name": "gin_trgm_ops"},
        ),
        # Oldest pending first; the usage ranking is computed, for the reasons
        # set out on `Institution`.
        Index(
            "ix_scholarship_programs_pending",
            "created_at",
            postgresql_where=text("status = 'pending_review'"),
        ),
        # Settled decision #100. The predicate above needs no change — it was
        # already a plain string — but the *rendered* index in the database moves
        # from `'pending_review'::lookup_status` to `::text`, which is why this
        # index is dropped and recreated by name rather than left alone.
        CheckConstraint(
            check_is_known("status", LookupStatus),
            name="status_is_known",
        ),
    )


class UserAward(TimestampMixin, Base):
    """Something a user won. Legacy ``Scholarship-Awards``, 17 rows.

    **User-level, not mentor-level.** A mentee can hold awards from day one —
    someone who won a scholarship and is now seeking a second degree is the
    ordinary case, not an edge one.

    ``scholarship_program_id`` is a **departure from the package**, and it is
    what gives the catalogue a consumer. The package pairs ``user_awards`` with
    a separate ``user_scholarship_experience`` table keyed on a
    ``relationship`` discriminator; that table does not ship, because the legacy
    field behind it has no option set and no values on any row. Without this
    column nothing in the schema would reference ``scholarship_programs`` at
    all.

    The shape is deliberately the one used by ``education_entries``:
    **``title`` is always kept and the link is optional.** "I won Chevening" and
    "I won Best Graduating Student at Unilag" become the same row, differing
    only in whether the catalogue happened to know the name. Display never
    depends on the link, so an unresolved one degrades filtering rather than the
    profile.

    **Nothing renders a checkmark.** ``verification_status`` defaults to
    ``UNVERIFIED`` and stays there this phase; the remaining columns exist so
    that switching verification on later is a feature flag rather than a
    migration.
    """

    __tablename__ = "user_awards"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # Named by hand: `verified_by` is a second foreign key to `users`, and the
    # convention renders on `column_0_name`.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_user_awards_user_id_users"),
        nullable=False,
    )

    institution: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    scholarship_program_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("scholarship_programs.id", ondelete="RESTRICT")
    )

    year: Mapped[int | None] = mapped_column()

    verification_status: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus), nullable=False, server_default=text("'unverified'")
    )
    evidence_url: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_user_awards_verified_by_users"),
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        Index("ix_user_awards_user", "user_id", postgresql_where=text("deleted_at IS NULL")),
        # Serves the derived usage ranking on the curation queue, which replaced
        # the stored `usage_count` this table would otherwise have incremented.
        Index("ix_user_awards_program", "scholarship_program_id"),
        # `now()` is not immutable and PostgreSQL permits it here anyway —
        # verified, not assumed. It is safe because the bound only ever grows: a
        # row valid when written stays valid at any later restore.
        CheckConstraint(
            "year IS NULL OR year BETWEEN 1950 AND EXTRACT(YEAR FROM now())::int + 1",
            name="year_is_sane",
        ),
        # Settled decision #100. Three of these four members ship with no
        # producer — nothing verifies an award yet — which is exactly the case
        # the handoff cites for why a droppable vocabulary matters.
        CheckConstraint(
            check_is_known("verification_status", VerificationStatus),
            name="verification_status_is_known",
        ),
    )

"""Scholarships: the catalogue of named funding schemes.

A **scholarship programme** is a named, externally-run funding scheme a student
applies to — Chevening, Fulbright, MasterCard Foundation. It exists
independently of any user, exactly like an institution. It is not a kind of
funding, not an amount, and not something one university invented for itself.

What links a *person* to one of these rows is deliberately not here yet. The
package specifies ``user_scholarship_experience`` with a
``relationship ∈ awarded | applied | advised`` discriminator, and that table
overlaps ``user_awards``: "I won Chevening" has two legal homes, one as free text
and one as a foreign key, with nothing choosing between them. Resolving it needs
the legacy ``Scholarship Experience`` option set, which is empty on every row of
the dev export — so it lands in a later pull request rather than being guessed
at. The catalogue below is unaffected either way, which is why it ships now.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import LookupStatus
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


class ScholarshipProgram(TimestampMixin, Base):
    """A named funding scheme, seeded with what is known and extended on demand.

    **The merge path is mandatory, not optional** (package D15). Without
    suggest-before-create and an admin merge, "Chevening", "chevening
    scholarship" and "Chevening Award" become three live rows inside a month and
    filtering by scholarship stops working. ``merged_into_id`` plus the trigram
    index below are what make that recoverable; ``usage_count`` is the signal
    that tells an approval from a typo.

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
        pg_enum(LookupStatus), nullable=False, server_default=text("'approved'")
    )

    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("scholarship_programs.id", ondelete="RESTRICT")
    )

    usage_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))

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
        Index(
            "ix_scholarship_programs_pending",
            text("usage_count DESC"),
            postgresql_where=text("status = 'pending_review'"),
        ),
    )

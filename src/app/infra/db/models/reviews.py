"""Reviews: what a mentee said about a session, on the scale they said it on.

One table, and one measurement that governs its shape:

    reviews    a mentee's account of one session with one mentor

**The four mentor ratings are a three-point scale, not a five-point one.** The
canonical DDL declares them ``int CHECK (BETWEEN 1 AND 5)``; Bubble stores the
mentee's three choices as ``1.67 / 3.34 / 5`` — ``1, 2, 3`` multiplied by
``5/3``. An ``int`` column cannot hold ``3.34``, so the package's declaration
breaks outright on the first migrated row, and that is the contradiction this
module resolves in favour of the measurement (ADR 0007: the package is a brain
dump, and where they disagree the measurement wins).

They are stored as the **ordinal the mentee actually chose** — ``1 = Not great``,
``2 = Great``, ``3 = Excellent``, which is what the form renders. Percentages and
the ``X/5`` a profile shows are derived at query time and never stored (D56).

    *Why not* ``numeric(3,2)`` *carrying the scaled values:* nobody ever chose
    ``3.34``. It is Bubble's presentation scaling baked into storage, and a
    ``numeric`` column also admits ``2.15``, which the product can neither
    produce nor interpret — the same defect as a vocabulary member with no
    producer. The mapping is lossless and reversible, and
    ``legacy_bubble_id`` keeps the join back to the row that carried ``3.34``.

    *A deliberate departure from settled decision #100*, which would make a
    closed set ``text`` + ``CHECK``. These stay ``smallint`` because the display
    must **average** them, and text moves that mapping into every query. The
    ``StrEnum`` still exists at the Pydantic boundary, so the API publishes
    ``"excellent"`` rather than a magic number.

**A CHECK cannot catch the legacy scaling, and it is important that nobody
believes otherwise.** ``3.34`` assigned to a ``smallint`` *rounds to 3* and
satisfies ``BETWEEN 1 AND 3``. A loader that casts rather than maps stores
*Excellent* where the mentee chose *Great*, silently, with every gate green.
The mapping belongs in the transform, explicitly;
``test_a_scaled_legacy_value_rounds_instead_of_failing`` is the executable form
of this paragraph.

**``session_id`` is nullable in the column and required at the boundary.** The
legacy ``Reviews`` type has no link to a session — confirmed from
``docs/bubble-data-model.md``, the Data API's ``⭐review`` and
``script-data-dev/review.json`` — so the 53 migrated rows have none and
``NOT NULL`` could not hold them. Every review the product writes has one. This
is the shape ``sessions.session_type_id`` already shipped in: nullable column,
``NOT NULL`` as a later contract migration once no row contradicts it.

**One foreign key to the session, not two.** The offering is reached by joining
``sessions.session_type_id``. A ``session_type_id`` here would be the same fact
twice (non-negotiable #8), and unlike a snapshotted rate it is not a mutable
value that has to be captured at the time.

**What the schema cannot enforce, and where it is enforced instead.**
``reviewed_for`` must equal ``sessions.mentor_id`` whenever ``session_id`` is
present. A ``CHECK`` cannot span two tables, so the invariant lives at the single
write path rather than in the database — the same honest split
``session_stats`` documents for ``sessions.status`` against
``session_participants.attendance_status``. Stating it here is the point: an
invariant nobody has written down is one every reader has to rediscover.

**Deletion policy, from ADR 0013 rather than copied from the package.** Cascade
where the child is meaningless without its parent and records no auditable fact;
restrict where it is evidence. A review is evidence *and* it is published on a
mentor's profile, so every foreign key here restricts.

**Soft delete, which ``sessions`` deliberately does not carry.** A cancelled
session is still a session, so nothing about it is ever withdrawn. Public review
text is different: it is the one thing on this platform that will need
moderation, and hard-deleting evidence contradicts the append-only rule as
directly as an overwrite would. ``deleted_at`` is how a review stops being
published without stopping being a record.
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import SessionRole
from app.domain.reviews import (
    MENTOR_RATINGS,
    ORDINAL_SCALE,
    RECOMMEND_SCALE,
    VALUABLE_SCALE,
)
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


def _within(column: str, bounds: tuple[int, int]) -> CheckConstraint:
    """A range constraint rendered from its bound, never typed out beside it."""
    low, high = bounds
    return CheckConstraint(f"{column} BETWEEN {low} AND {high}", name=f"{column}_range")


class Review(Base, TimestampMixin):
    """One mentee's account of one session, appended and never overwritten.

    **Append, never overwrite** — the legacy app kept one row per
    mentee/mentor pair and updated it. A review is about a *session*, so editing
    session A's row because the mentee attended session B leaves a row claiming
    to be about A while describing B. The uniqueness rule below is one review
    *per session*, which is the same rule stated as a constraint, and the profile
    renders a dated list, which shows a trajectory under append and silently
    rewrites history under edit.

    The concern edit solves — one mentee dominating a mentor's average — is a
    **display** problem. Cap or recency-weight the aggregate; do not destroy
    rows. This project already ruled the same way once, when D8 chose a ledger
    over a counter because a counter leaves "no record".
    """

    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    #: Null on the 53 migrated rows and on nothing the product writes. Fabricating
    #: a link from `reviewedBy` + `reviewedFor` + proximity is irreversible — 8 of
    #: 12 dev pairs have more than one booking — where a later backfill is
    #: additive.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="RESTRICT")
    )
    #: The author. `Creator` equals `reviewedBy` in the one dev row, so this
    #: needs no fallback — re-checked at the production export.
    reviewed_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_for: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    #: **What ``reviewed_for`` is not.** That column names a user, and a user is a
    #: mentor to one person and a mentee to another; this names the capacity they
    #: were reviewed in. It is not derivable the way the offering is: the migrated
    #: rows have no session to join, and the day a mentor reviews a mentee every
    #: aggregate filtering on ``reviewed_for`` alone starts counting the wrong
    #: rows. `SessionRole` rather than a vocabulary of its own — the enum already
    #: ships, and one shared vocabulary carries one CHECK per column it guards.
    reviewed_for_role: Mapped[SessionRole] = mapped_column(
        str_enum(SessionRole), nullable=False, server_default=text("'mentor'")
    )

    communication_rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    knowledge_rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    practicality_rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    support_rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    valuable_rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    nps_recommend_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    #: "Public review (required)" — the form will not submit without it, so the
    #: column says so. The canonical DDL leaves it nullable; the measurement is
    #: the screen.
    public_review: Mapped[str] = mapped_column(Text, nullable=False)
    #: "Improvement Feedback (optional)" — the only optional field on the form,
    #: and about the *platform* rather than the mentor. Never published.
    private_review: Mapped[str | None] = mapped_column(Text)

    #: Withdrawn rather than destroyed. Read paths filter on it; the row stays.
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    #: What makes the load re-runnable and decision 1 reversible.
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        *(_within(column, ORDINAL_SCALE) for column in MENTOR_RATINGS),
        _within("valuable_rating", VALUABLE_SCALE),
        _within("nps_recommend_score", RECOMMEND_SCALE),
        CheckConstraint(
            check_is_known("reviewed_for_role", SessionRole),
            name="reviewed_for_role_is_known",
        ),
        CheckConstraint("reviewed_by <> reviewed_for", name="no_self_review"),
        # **Partial, and the predicate is the point.** A plain UNIQUE would admit
        # the migrated rows too, because PostgreSQL treats NULLs as distinct —
        # but it would do so by accident of a default, where this says which rows
        # the rule is about. `NULLS NOT DISTINCT` in a later PostgreSQL would
        # silently invert a plain constraint; it cannot touch this one.
        Index(
            "uq_reviews_one_per_session_author",
            "session_id",
            "reviewed_by",
            unique=True,
            postgresql_where=text("session_id IS NOT NULL"),
        ),
        # The discovery card and the profile both read this, shipped ahead of
        # either — `ix_sessions_mentor_completed` is the precedent, a partial
        # index that existed from the M4 schema and had no reader until the card
        # arrived. Both halves of the predicate are load-bearing: a withdrawn
        # review must not move a mentor's average, and a mentee-directed one must
        # not enter it at all.
        Index(
            "ix_reviews_mentor_valuable",
            "reviewed_for",
            "valuable_rating",
            postgresql_where=text("deleted_at IS NULL AND reviewed_for_role = 'mentor'"),
        ),
        # The 30-day window: *has this mentee reviewed this mentor recently*, which
        # is answered by the newest row for the pair and nothing else.
        Index(
            "ix_reviews_author_subject_created",
            "reviewed_by",
            "reviewed_for",
            text("created_at DESC"),
        ),
    )

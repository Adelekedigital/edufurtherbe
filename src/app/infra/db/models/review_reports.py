"""Asking somebody to look at a review, and what they decided.

    review_reports    the subject flagged a review; an admin adjudicated

**The subject reports; an admin decides. That split is the whole design.** A
mentor who could hide reviews they dislike would make the rating meaningless,
and a mentee reading a five-star profile would be misled — which is precisely
what the review system exists to prevent. So nothing here removes anything: a
report is a *request for adjudication*, and only an upheld outcome sets
``reviews.deleted_at``.

**The review stays visible while it is pending**, deliberately. Hiding on
report is the same power under another name — file a report against every
unflattering review and they all vanish while nobody looks.

WHY THE MECHANISM ALREADY EXISTS
================================
``reviews.deleted_at`` is already there and its own docstring says it is *for*
moderation — *"hard-deleting evidence contradicts the append-only rule"*. The
profile's partial index is ``WHERE deleted_at IS NULL AND reviewed_for_role =
'mentor'``, so a soft-deleted review leaves the public list **and** the average
without either read learning about moderation. This table adds the *reason* and
the *record*, not the removal.
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ReviewReportOutcome, ReviewReportReason
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class ReviewReport(Base, TimestampMixin):
    """One complaint about one review, and its adjudication.

    **Not append-only.** A report is resolved in place. Writing a second row for
    the decision would make "is this pending" an aggregate over history instead
    of a lookup, and the admin queue asks that question on every page.
    """

    __tablename__ = "review_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    #: **No single-column foreign key.** The composite in `__table_args__` does
    #: the work — see there for why.
    review_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    #: The subject of the review, and the only person who may file this.
    reported_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    reason: Mapped[ReviewReportReason] = mapped_column(str_enum(ReviewReportReason), nullable=False)

    #: What the reporter wants the admin to know, in their own words. Optional:
    #: `not_this_session` is checkable without prose, and requiring an
    #: explanation for every report is how a queue fills with "see above".
    detail: Mapped[str | None] = mapped_column(Text)

    #: The three resolution columns move together — see the CHECK below.
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    outcome: Mapped[ReviewReportOutcome | None] = mapped_column(str_enum(ReviewReportOutcome))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        # **Only the subject of a review may report it**, and that is structural
        # rather than a check somebody can forget. A single-column key on
        # `reviews.id` is satisfied by *any* review — a stranger, or the
        # review's own author, could file against one that has nothing to do
        # with them and the admin queue would carry it.
        #
        # `session_types.conferencing_option_id` states the rule this follows:
        # a single-column key is satisfied by any row, including another
        # person's. Fourth time that shape has been the right answer here.
        #
        # It also catches the author case for free: an author who regrets a
        # review withdraws it. Reporting is the subject's channel, and
        # conflating them would let an author route their own text through
        # moderation.
        ForeignKeyConstraint(
            ["review_id", "reported_by"],
            ["reviews.id", "reviews.reviewed_for"],
            name="fk_review_reports_report_belongs_to_subject",
            ondelete="RESTRICT",
        ),
        # Reporting the same review twice is a duplicate, not a second
        # complaint — and a queue carrying both is adjudicated twice.
        UniqueConstraint("review_id", "reported_by", name="uq_review_reports_review_id_reporter"),
        CheckConstraint(check_is_known("reason", ReviewReportReason), name="reason_is_known"),
        # Nullable, so the CHECK admits NULL: `reason IS NULL` is never true
        # here, but `outcome` is null for every pending report.
        CheckConstraint(
            f"outcome IS NULL OR {check_is_known('outcome', ReviewReportOutcome)}",
            name="outcome_is_known",
        ),
        # **All three or none.** A report with an outcome and no timestamp is
        # neither pending nor closed, and every filter over the queue would have
        # to guess which. `resolved_by` is in it because somebody decided this,
        # and a moderation record that cannot say who is not a record.
        CheckConstraint(
            "num_nonnulls(resolved_at, outcome, resolved_by) IN (0, 3)",
            name="resolution_is_whole",
        ),
        # The queue: what is still waiting. Partial, because a resolved report
        # is history and the queue is only ever asked for the open ones.
        Index(
            "ix_review_reports_pending",
            text("created_at"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
        # Every report against one review, for the admin reading a single case.
        Index("ix_review_reports_review", "review_id"),
    )

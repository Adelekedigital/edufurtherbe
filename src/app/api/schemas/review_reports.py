"""Reporting a review, and how the subject sees the reviews about them."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ReviewReportOutcome, ReviewReportReason


class ReviewReportWrite(BaseModel):
    """Asking somebody to look at a review of yourself.

    **A request, never an action.** Filing this hides nothing: the review stays
    on the profile and in the average until an admin upholds the report. Hiding
    on report would be the same power as hiding outright, exercised one
    complaint at a time.
    """

    model_config = ConfigDict(extra="forbid")

    reason: ReviewReportReason
    #: What the admin should know, in the reporter's words. Optional, because
    #: `not_this_session` is checkable without prose and demanding an
    #: explanation for every report is how a queue fills with "see above".
    #:
    #: Bounded so a report cannot carry an essay into a moderation view that
    #: renders it.
    detail: str | None = Field(default=None, max_length=2000)


class ReviewReportRead(BaseModel):
    """A report the caller filed.

    No `resolved_by`: the reporter learns *what* was decided, not which admin
    decided it. Naming the moderator to the person they ruled against invites
    exactly the pressure moderation exists to absorb.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reason: ReviewReportReason
    created_at: datetime
    resolved_at: datetime | None = None
    outcome: ReviewReportOutcome | None = None


class OwnReviewRead(BaseModel):
    """One review about the caller, as its subject sees it.

    **Carries `withdrawn`, which the public list cannot.** A review that has
    gone is absent from the profile — that is the whole point of withdrawal —
    but the subject is the one person who needs to see that it went, and
    `report` says whether that was their doing or the author's.

    The author is a first name and an initial, exactly as on the public page.
    The subject does not get a fuller name than a stranger does.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    public_review: str
    #: This review's own `valuable_rating`, the `X/5` badge shown beside it.
    session_value: int
    #: True once the review has left the profile, whoever removed it.
    withdrawn: bool
    author_first_name: str | None = None
    author_last_initial: str | None = None
    author_institution: str | None = None
    #: The caller's own report, if they filed one. Null is "not reported",
    #: which is a different state from reported-and-dismissed.
    report: ReviewReportRead | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OwnReviewRead:
        """Flat row in, nested model out.

        The join returns the report's columns prefixed rather than nested, and
        `report_id` being null is what says there is no report — a `LEFT JOIN`
        cannot express absence any other way.
        """
        report = (
            ReviewReportRead(
                id=row["report_id"],
                reason=row["report_reason"],
                created_at=row["report_created_at"],
                resolved_at=row["report_resolved_at"],
                outcome=row["report_outcome"],
            )
            if row.get("report_id") is not None
            else None
        )
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            public_review=row["public_review"],
            session_value=row["valuable_rating"],
            withdrawn=row["withdrawn"],
            author_first_name=row.get("author_first_name"),
            author_last_initial=row.get("author_last_initial"),
            author_institution=row.get("author_institution"),
            report=report,
        )

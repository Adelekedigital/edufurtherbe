"""What the review queues look like, and what a decision carries.

These are the only response models in the service that show one user another
user's data by design. They stay narrow anyway: the queue needs enough to make a
decision and nothing more, and an admin screen is still a screen somebody can be
shoulder-surfing.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.api.schemas.common import Normalised
from app.domain.admin_notes import NOTE_BODY_MAX_LENGTH
from app.domain.enums import CoverColor, FeaturedSource
from app.domain.session_type_limit import SESSION_TYPE_LIMIT_CEILING


class PendingInstitutionRead(BaseModel):
    """An institution awaiting review, and how many people are waiting on it.

    ``uses`` is **computed, never stored** — the count of live education entries
    and goal schools pointing here. It is what tells the reviewer which action to take: eight
    entries on one spelling is an approval, one entry plus a typo is a merge.
    `usage_count` was a stored column that nothing incremented, so it read zero
    on every row forever and its index sorted a constant.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    uses: int
    created_at: datetime
    #: Who typed it. Never null for a `manual` row — a `CHECK` guarantees it —
    #: and it is who the reviewer would ask if the name is ambiguous.
    created_by: UUID | None = None


class PendingMentorRead(BaseModel):
    """An application awaiting a decision."""

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    first_name: str | None = None
    last_name: str | None = None
    email: str
    headline: str | None = None
    years_of_experience: int | None = None
    created_at: datetime
    #: When the application was submitted; the queue is ordered by it.
    submitted_at: datetime | None = None


class MergeRequest(Normalised):
    """Where a duplicate's entries should go."""

    winning_id: UUID


class DeclineRequest(Normalised):
    """Why an application was declined.

    **Optional.** A reason is better for the applicant and for whoever reads the
    record later, but requiring one turns a clear-cut decline into a form to
    fight with, and an admin typing "no" to satisfy a validator has told nobody
    anything.
    """

    reason: str | None = Field(default=None, max_length=1000)


class ListingRequest(Normalised):
    """Why a mentor is being unlisted: **required to unlist** (decision 298),
    read by other admins and never shown to the mentor. Ignored when listing."""

    reason: str | None = Field(default=None, max_length=1000)


class StatusEventRead(BaseModel):
    """One transition in a mentor's history."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status_type: str
    reason: str | None = None
    created_at: datetime
    #: Null for rows the backfill wrote, where nobody made the decision.
    created_by: UUID | None = None
    created_by_email: str | None = None


class FeaturedWrite(BaseModel):
    """The mentor an admin features for a week."""

    model_config = ConfigDict(extra="forbid")

    mentor_id: UUID = Field(description="A mentor who is bookable now.")


class FeaturedWeekRead(BaseModel):
    """One week of "Featured this week", as the admin schedule shows it."""

    week_start: date = Field(description="The week's Monday, UTC.")
    mentor_id: UUID
    first_name: str | None = None
    last_name: str | None = None
    slug: str | None = None
    source: FeaturedSource = Field(
        description="`automatic` for the rotation's own pick, `admin` for an override."
    )
    chosen_by: UUID | None = Field(
        default=None, description="The admin who chose the week; null for the rotation."
    )
    bookable: bool = Field(
        description=(
            "Whether the mentor can be booked now. A future choice who has since "
            "paused is skipped when the week arrives, and the rotation fills it."
        )
    )


class InstitutionApprovedRead(BaseModel):
    """An institution was approved."""

    approved: bool = Field(description="Always true: the institution is now selectable.")


class InstitutionMergedRead(BaseModel):
    """A duplicate institution was merged into its winner."""

    entries_moved: int = Field(description="Education entries repointed onto the winner.")


class MentorDecidedRead(BaseModel):
    """A mentor application was decided."""

    decided: bool = Field(description="Always true: the decision was recorded.")


class MentorListingChangedRead(BaseModel):
    """A mentor's listing was moved."""

    changed: bool = Field(description="Always true: the listing change was recorded.")


class FeaturedWeekSetRead(BaseModel):
    """The week whose featured mentor was chosen."""

    week_start: date = Field(description="The week's Monday, UTC.")


class SessionTypeLimitWrite(BaseModel):
    """`PUT /admin/mentors/{user_id}/session-type-limit`'s body (decision 286)."""

    model_config = ConfigDict(extra="forbid")

    #: Required although nullable: null resets, so an omitted field must be a
    #: `422` rather than a reset nobody asked for.
    #: Strict: `true`, `"7"` and `7.0` are a `422`, not a coerced limit.
    limit: StrictInt | None = Field(
        ge=1,
        le=SESSION_TYPE_LIMIT_CEILING,
        description=(
            f"How many session types this mentor may hold, 1 to {SESSION_TYPE_LIMIT_CEILING}. "
            "`null` resets to the platform default."
        ),
    )


class SessionTypeLimitRead(BaseModel):
    """The mentor's limit after the write."""

    limit: int = Field(description="The limit now in force: the override, or the default.")
    is_default: bool = Field(description="Whether `limit` is the platform default (no override).")


class AdminNoteWrite(Normalised):
    """An internal note on a mentor: what an admin wants the others to know."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=NOTE_BODY_MAX_LENGTH)


class AdminNoteAuthorRead(BaseModel):
    """Who wrote a note. Names and photo are null once that admin's account is gone."""

    user_id: UUID
    first_name: str | None
    last_name: str | None
    avatar_url: str | None
    cover_color: CoverColor | None


class AdminNoteRead(BaseModel):
    """One internal note; only admins ever read these."""

    id: UUID
    body: str
    created_at: datetime
    author: AdminNoteAuthorRead

    @classmethod
    def from_row(cls, row: dict[str, object]) -> AdminNoteRead:
        return cls(
            id=row["id"],  # type: ignore[arg-type]
            body=str(row["body"]),
            created_at=row["created_at"],  # type: ignore[arg-type]
            author=AdminNoteAuthorRead(
                user_id=row["author_id"],  # type: ignore[arg-type]
                first_name=row["author_first_name"],  # type: ignore[arg-type]
                last_name=row["author_last_name"],  # type: ignore[arg-type]
                avatar_url=row["author_avatar_url"],  # type: ignore[arg-type]
                cover_color=row["author_cover_color"],  # type: ignore[arg-type]
            ),
        )


#: The most applications one bulk approve may carry.
MAX_BULK_DECISIONS = 50


class BulkDecisionWrite(BaseModel):
    """The checked applications to approve. Approval only: there is no bulk
    decline, and an unknown field such as `approve` is refused."""

    model_config = ConfigDict(extra="forbid")

    user_ids: list[UUID] = Field(min_length=1, max_length=MAX_BULK_DECISIONS)


class BulkDecisionRead(BaseModel):
    """What a bulk approve came to, each id once, in the order sent."""

    approved: list[UUID] = Field(description="Approved, listed and emailed now.")
    failed: list[UUID] = Field(
        description=(
            "Not approved: no such mentor, no application still in review "
            "(a draft, or one already decided), or an error on that mentor alone."
        )
    )

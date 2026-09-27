"""What the review queues look like, and what a decision carries.

These are the only response models in the service that show one user another
user's data by design. They stay narrow anyway: the queue needs enough to make a
decision and nothing more, and an admin screen is still a screen somebody can be
shoulder-surfing.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas.common import Normalised
from app.domain.enums import FeaturedSource


class PendingInstitutionRead(BaseModel):
    """An institution awaiting review, and how many people are waiting on it.

    ``uses`` is **computed, never stored** — the count of live education entries
    pointing here. It is what tells the reviewer which action to take: eight
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

"""The review contract: three-point answers in, three-point answers out.

**The wire speaks words, the column stores numbers.** A client sends
`"excellent"` and the row holds `3`. That split is settled decision #100's
reasoning applied at the only layer where it does not cost anything: the display
has to *average* these, so text in the column would move the mapping into every
query — but a magic `3` on the wire is a number whose meaning lives in a
document rather than in the payload.

**No `reviewed_for`.** The mentor is read from the session inside the write.
Accepting it would let a request carry two answers to "who is this about" with
no rule for which wins, and would let a caller aim a review at somebody who
never mentored them. Same reasoning `SessionBookingWrite` gives for refusing
`mentor_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.common import Normalised
from app.domain.reviews import (
    MENTOR_RATINGS,
    RECOMMEND_SCALE,
    VALUABLE_SCALE,
    MentorRating,
    from_ordinal,
    to_ordinal,
)

#: Unpacked once so the bounds below read as numbers rather than as subscripts,
#: and still come from the single declaration in `domain/reviews.py`.
VALUABLE_MIN, VALUABLE_MAX = VALUABLE_SCALE
RECOMMEND_MIN, RECOMMEND_MAX = RECOMMEND_SCALE

#: The longest a free-text answer may be. Generous — a review is prose somebody
#: took trouble over — and present because an unbounded `text` column is a body
#: nobody sized, which the request-size middleware would refuse far less
#: helpfully than a field error naming the field.
MAX_REVIEW_LENGTH = 4000


class ReviewWrite(Normalised):
    """What a mentee sends to review one completed session.

    Every field is required except the last, which is what the form asks: steps
    1 and 2 will not advance until they are answered, and step 3 says
    "(optional)" in its own label.
    """

    session_id: UUID = Field(
        description=(
            "Which completed session this is about. The mentor follows from it.\n\n"
            "**Always supplied, by both paths.** A review request links straight "
            "to the session, and the profile's Reviews tab resolves it from "
            "`GET /me/reviewable-sessions` — silently when one session is "
            "eligible, by asking when more than one is."
        )
    )

    communication_rating: MentorRating = Field(
        description="How clearly the mentor communicated their ideas and advice."
    )
    knowledge_rating: MentorRating = Field(
        description="How knowledgeable the mentor was on the topics discussed."
    )
    practicality_rating: MentorRating = Field(description="How practical the suggestions were.")
    support_rating: MentorRating = Field(
        description="How supported the mentee felt during the session."
    )

    valuable_rating: int = Field(
        ge=VALUABLE_MIN,
        le=VALUABLE_MAX,
        description=(
            "How valuable the session was in moving the mentee closer to their "
            "study-abroad goals. `1` is *not valuable at all*, `5` is *extremely "
            "valuable*. **This is the figure a mentor's card shows as `X/5`.**"
        ),
    )
    nps_recommend_score: int = Field(
        ge=RECOMMEND_MIN,
        le=RECOMMEND_MAX,
        description=(
            "How likely the mentee is to recommend this mentor, `1` to `10`. "
            "**There is no `0`** — the control renders ten buttons starting at "
            "one, so nothing can produce one."
        ),
    )

    public_review: str = Field(
        min_length=1,
        max_length=MAX_REVIEW_LENGTH,
        description="Shown on the mentor's profile, attributed to the mentee. Required.",
    )
    private_review: str | None = Field(
        default=None,
        max_length=MAX_REVIEW_LENGTH,
        description=(
            "How the *platform* could have been better. Optional, and **never "
            "published** — it is not about the mentor and they never see it."
        ),
    )

    def to_columns(self) -> dict[str, Any]:
        """The row this request becomes, with each word turned back into its point."""
        values: dict[str, Any] = {
            "session_id": self.session_id,
            "valuable_rating": self.valuable_rating,
            "nps_recommend_score": self.nps_recommend_score,
            "public_review": self.public_review,
            "private_review": self.private_review,
        }
        for column in MENTOR_RATINGS:
            values[column] = to_ordinal(getattr(self, column))
        return values


#: The fields an edit may *omit* but may not *empty*. Every one of them is
#: `NOT NULL` on the table, so a `null` reaching the `UPDATE` is a
#: `NotNullViolationError` — which is not an `AppError`, so it leaves as a `500`
#: for a request the product has a clean `422` to give.
#:
#: `private_review` is deliberately absent: it is the one genuinely nullable
#: column, and clearing it is what the route means by "sending null clears it".
NOT_NULLABLE = frozenset(
    {*MENTOR_RATINGS, "valuable_rating", "nps_recommend_score", "public_review"}
)


class ReviewEdit(Normalised):
    """A correction inside the ten-minute compose window. Every field optional.

    **`session_id` is absent, and that is the point.** Editing it would move the
    review to a different session and step around every eligibility clause —
    write a review of a session you may review, then repoint it at one you may
    not. The window is for fixing a typo, not for changing what was reviewed.
    """

    communication_rating: MentorRating | None = None
    knowledge_rating: MentorRating | None = None
    practicality_rating: MentorRating | None = None
    support_rating: MentorRating | None = None
    valuable_rating: int | None = Field(default=None, ge=VALUABLE_MIN, le=VALUABLE_MAX)
    nps_recommend_score: int | None = Field(default=None, ge=RECOMMEND_MIN, le=RECOMMEND_MAX)
    public_review: str | None = Field(default=None, min_length=1, max_length=MAX_REVIEW_LENGTH)
    private_review: str | None = Field(default=None, max_length=MAX_REVIEW_LENGTH)

    @model_validator(mode="after")
    def _no_emptied_columns(self) -> ReviewEdit:
        """Refuse an explicit `null` where the column cannot hold one.

        **Absent and null are different things here**, which is why the types
        stay `| None`: not sending a field leaves it alone, and `exclude_unset`
        is what tells the two apart. What this refuses is the *sent* null.

        `"   "` arrives here as `None` too — `Normalised` trims it and turns an
        emptied string into null before validation, and `min_length` guards only
        the `str` branch of `str | None`. So whitespace and an explicit null are
        one case, and they get one answer.
        """
        emptied = sorted(
            field for field in self.model_fields_set & NOT_NULLABLE if getattr(self, field) is None
        )
        if emptied:
            message = f"these fields may be omitted but not emptied: {', '.join(emptied)}"
            raise ValueError(message)
        return self

    def to_columns(self) -> dict[str, Any]:
        """Only what was actually sent.

        ``exclude_unset`` rather than dropping ``None``s: `private_review` is
        genuinely nullable, so *sent as null* means "clear it" and *not sent*
        means "leave it". Collapsing the two would make the optional field the
        one field an edit could never remove.
        """
        sent = self.model_dump(exclude_unset=True)
        return {
            column: (to_ordinal(value) if column in MENTOR_RATINGS and value is not None else value)
            for column, value in sent.items()
        }


class ReviewRead(BaseModel):
    """One review, as the author and the mentor's profile both see it.

    ``private_review`` is **absent from this model deliberately**, not merely
    unset: it is feedback about the platform, and a read model that carries it is
    one refactor away from rendering it on a profile.
    """

    id: UUID
    session_id: UUID | None
    reviewed_for: UUID

    communication_rating: MentorRating
    knowledge_rating: MentorRating
    practicality_rating: MentorRating
    support_rating: MentorRating
    valuable_rating: int
    nps_recommend_score: int
    public_review: str

    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ReviewRead:
        """Each stored point turned back into the word the mentee chose."""
        values = dict(row)
        for column in MENTOR_RATINGS:
            values[column] = from_ordinal(int(values[column]))
        return cls.model_validate(values)


class ReviewableSessionRead(BaseModel):
    """A session this mentee may review right now.

    What the profile's Reviews tab needs to decide whether to ask. Empty means
    there is nothing to review; one means write straight away; more than one
    means show the choice.
    """

    session_id: UUID
    mentor_id: UUID
    starts_at: datetime
    session_type_id: UUID | None = Field(
        default=None,
        description="Null on a migrated session, which predates offerings.",
    )
    session_type_name: str | None = Field(
        default=None, description="What the mentee booked, for a picker to label the row."
    )

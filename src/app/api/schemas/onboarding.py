"""What onboarding returns, the step a client records, and where the caller stands."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.onboarding import (
    OnboardingRequirement,
    OnboardingStep,
    ProfileEvidence,
    missing_requirements,
    published_step,
)


class OnboardingRead(BaseModel):
    """The caller's onboarding record, as completion and a saved step return it.

    ``granted`` is deliberately **not** here. Whether this particular call
    created the credit is transport-level information — it is the difference
    between 201 and 200 — and a client that branched on a body field instead
    would be reading the status code twice. The balance itself lives on
    ``GET /api/v1/me``, which is the one place it is published.
    """

    model_config = ConfigDict(from_attributes=True)

    #: **Nullable, because the column is.** The ETL writes rows with a null
    #: `completed_at` for legacy users who were mid-onboarding at cutover, so a
    #: required field here turned `GET /me/onboarding` into a 500 for exactly
    #: those users — the ones most likely to be sent back to finish. Found by
    #: code review.
    #:
    #: A null means started but not finished, which the 404 for *never started*
    #: keeps distinct.
    completed_at: datetime | None = None
    #: One of `domain.onboarding.STEPS` when the API wrote it; a migrated row
    #: may still hold a legacy value ("3".."6"), so the read stays free text.
    #: Null for anybody who never recorded where they got to.
    last_step: str | None = None


class OnboardingStepWrite(BaseModel):
    """Where the caller got to (decision 260).

    ``last_step`` only: ``completed_at`` belongs to the completion endpoint,
    which checks the profile and pays a credit, so it is refused here as an
    extra field rather than ignored.
    """

    model_config = ConfigDict(extra="forbid")

    #: Exactly one of `domain.onboarding.STEPS`, case-sensitive. Typed with the
    #: domain's `Literal`, so the check and the published enum cannot disagree.
    last_step: OnboardingStep


class OnboardingStatusRead(BaseModel):
    """Where the caller stands on onboarding, for `GET /me` (decision 261).

    `complete` is whether they finished (`completed_at` is set). `missing` is
    what the mentee rule still lacks, in step order, by the same rule the
    completion endpoint refuses on. The two are separate facts: a user who
    finished under an earlier rule stays complete while `missing` names what
    the current one would ask.
    """

    complete: bool = Field(description="Whether onboarding is finished.")
    missing: list[OnboardingRequirement] = Field(
        description=(
            "The mentee answers still owed, in step order: `help` (at least one "
            "help area), `origin`, `degree` (a degree goal) and `field` (a field of "
            "study). Empty when all are given, and always empty for a mentor, whose "
            "bar has no mentee answers. Route on `complete`; when sending a mentee "
            "to finish, open the first missing step."
        )
    )

    #: Decision 268. Optional in the spec, like every added field.
    last_step: OnboardingStep | None = Field(
        default=None,
        description=(
            "The step the caller last saved (`PUT /me/onboarding`), or `null` when "
            "they never saved one in this flow. A step carried from the old platform "
            "reads as `null`, which tells a migrated mentee from one who came back."
        ),
    )

    @classmethod
    def of(
        cls, evidence: ProfileEvidence, completed: bool, last_step: str | None
    ) -> OnboardingStatusRead:
        return cls(
            complete=completed,
            missing=list(missing_requirements(evidence)),
            last_step=published_step(last_step),
        )


class MentorApplicationRead(BaseModel):
    """A submitted mentor application: when, and where it stands."""

    submitted_at: datetime
    approval_status: str = Field(description="`pending` until an admin decides.")


class ApprovalSeenRead(BaseModel):
    """When the mentor first saw the approved screen."""

    approved_seen_at: datetime

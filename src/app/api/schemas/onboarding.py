"""What onboarding returns, and the step a client records."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.domain.onboarding import OnboardingStep


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

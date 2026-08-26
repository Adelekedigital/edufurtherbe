"""What finishing onboarding returns."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OnboardingRead(BaseModel):
    """The caller's onboarding record after completion.

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
    #: Free text, and the onboarding flow owns its values. Null for anybody who
    #: finished without the client recording where they got to.
    last_step: str | None = None

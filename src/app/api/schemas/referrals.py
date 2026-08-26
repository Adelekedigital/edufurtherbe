"""What an invite looks like on the wire."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ReferralWrite(BaseModel):
    """Sending an invite.

    ``invitee_email`` is optional: a shared link has no addressee, and the code
    is what attributes an arrival either way. When present it is lowercased
    here, matching ``NormalisedEmail`` — the column carries a lowercase CHECK
    and normalising in the handler is the version that works until somebody
    adds a second route.
    """

    model_config = ConfigDict(extra="forbid")

    invitee_email: EmailStr | None = None

    @field_validator("invitee_email")
    @classmethod
    def normalise(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None


class ReferralClaim(BaseModel):
    """Claiming an invite you arrived with."""

    model_config = ConfigDict(extra="forbid")

    #: Bounded so a claim cannot be used to probe with arbitrarily long input —
    #: the comment said so before the constraint did, which code review caught.
    #: Generated codes are 22 characters; the ceiling is generous rather than
    #: exact, because raising the entropy must not break existing clients.
    code: str = Field(min_length=1, max_length=128)


class ReferralRead(BaseModel):
    """One invite, and how far the person it named got.

    **No `status` field**, because there is no status column — it is derivable
    from the two timestamps, and publishing a computed one here would put the
    same rule in a third place. A client reads `signed_up_at` and
    `qualified_at`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    invitee_email: EmailStr | None = None
    invited_at: datetime
    #: **Set when the invitee claims their code, not when they signed up.** This
    #: service never sees a signup — `users` rows come from the provisioning CLI
    #: and the ETL — so this is the first moment it can witness.
    signed_up_at: datetime | None = None
    #: Set when the invitee finishes onboarding, which is what opens the
    #: referrer's recurring grant.
    qualified_at: datetime | None = None


class ReferralClaimRead(BaseModel):
    """The invite you just claimed.

    Narrower than :class:`ReferralRead` on purpose: the claimant is not the
    referrer, and `invitee_email` and `qualified_at` are the referrer's view of
    their own funnel rather than anything the invitee is owed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    invited_at: datetime
    signed_up_at: datetime | None = None

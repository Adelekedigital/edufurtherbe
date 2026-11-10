"""Where an account came from, and its analytics-cookie answer, as a client sends and reads them."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.domain.attribution import CONSENT_VERSION_MAX_LENGTH
from app.domain.enums import SignupChannel, SignupSource

__all__ = ["AcquisitionRead", "AcquisitionWrite", "AnalyticsConsentRead", "AnalyticsConsentWrite"]

#: Long enough for any tag a campaign link carries; a longer value is cut.
_RAW_MAX_LENGTH = 200

#: Stands in for a value of the wrong type: no vocabulary holds it, so it is
#: coerced like any unknown word.
_UNREADABLE = "?"


def _untrusted(value: object) -> object:
    """A string cut to length, ``None`` kept, anything else unreadable."""
    if value is None:
        return None
    return value[:_RAW_MAX_LENGTH] if isinstance(value, str) else _UNREADABLE


def _code(value: object) -> object:
    """A code is a string or nothing: one of another type names no invite."""
    return value[:_RAW_MAX_LENGTH] if isinstance(value, str) else None


Untrusted = Annotated[str | None, BeforeValidator(_untrusted)]


class AcquisitionWrite(BaseModel):
    """What the visitor arrived with, as the client held it across sign-up.

    **Untrusted, so never refused for its values**: an unknown `source` is
    recorded as `direct` and an unknown `channel` as `other`, a value of
    another type is unknown, and a string is cut to 200 characters.
    """

    source: Untrusted = Field(
        default=None,
        description=(
            "`invite`, `campaign`, `referral_site` or `direct`; anything else, "
            "`migrated` included, is recorded as `direct`. A `referral_code` "
            "naming somebody's live invite records `invite` whatever this says."
        ),
    )
    channel: Untrusted = Field(
        default=None,
        description=(
            "The campaign's channel (`utm_source`): `linkedin`, `google`, "
            "`whatsapp`, `instagram` or `newsletter`; anything else is `other`, "
            "and none is none."
        ),
    )
    referral_code: Annotated[str | None, BeforeValidator(_code)] = Field(
        default=None,
        description=(
            "The invite code the visitor arrived with, if any. Read to decide "
            "`invite`, then dropped: never stored, never sent on. Claiming the "
            "invite is still `POST /me/referrals/claim`."
        ),
    )


class AcquisitionRead(BaseModel):
    """What is recorded for the caller."""

    source: SignupSource = Field(
        description="How the account arrived; `migrated` for one moved from the old platform."
    )
    channel: SignupChannel | None = Field(description="The campaign's channel, or null.")


class AnalyticsConsentWrite(BaseModel):
    """The caller's answer to analytics cookies."""

    model_config = ConfigDict(extra="forbid")

    granted: bool = Field(description="`true` to allow analytics cookies, `false` to decline.")
    version: str = Field(
        min_length=1,
        max_length=CONSENT_VERSION_MAX_LENGTH,
        description="Which consent text was answered, as the client names it.",
    )


class AnalyticsConsentRead(BaseModel):
    """The caller's current answer."""

    granted: bool
    version: str
    answered_at: datetime = Field(description="When this answer was given.")

    @classmethod
    def from_row(cls, row: Any) -> AnalyticsConsentRead:
        return cls(granted=row.granted, version=row.version, answered_at=row.updated_at)

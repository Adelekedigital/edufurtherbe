"""Request bodies for a mentor profile link's visits and shares (decision 347)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ShareMethod

#: The longest hint accepted at all. Anything a real link or referrer carries
#: is far shorter; between this and what is kept, a value is dropped, not refused.
MAX_HINT = 2048


class VisitWrite(BaseModel):
    """What the profile page knows about how its visitor arrived."""

    model_config = ConfigDict(extra="forbid")

    via: str | None = Field(
        default=None,
        max_length=MAX_HINT,
        description=(
            "The `?via=` the profile link carried: `copy`, `share` or `invite`. Any "
            "other value up to 2,048 characters is stored as null, never refused."
        ),
    )
    referrer_host: str | None = Field(
        default=None,
        max_length=MAX_HINT,
        description=(
            "The host of `document.referrer`, e.g. `l.instagram.com`. Lowercased, "
            "with a port and a leading `www.` taken off; anything that is not a "
            "host name of at most 253 characters is stored as null (over 2,048 "
            "characters is a `422`)."
        ),
    )


class ShareWrite(BaseModel):
    """How a profile link was shared."""

    model_config = ConfigDict(extra="forbid")

    method: ShareMethod = Field(
        description="`copy` (to the clipboard) or `native` (the device's share sheet)."
    )
    own: bool | None = Field(
        default=None,
        description=(
            "Accepted for parity with the client's `link_shared` event and not "
            "stored: whether the mentor shared their own profile is decided from "
            "the bearer token."
        ),
    )

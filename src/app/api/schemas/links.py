"""A user's own links: what `/me/links` takes and what every read returns (decision 275)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas.common import Normalised
from app.domain.enums import LinkPlatform
from app.domain.social_links import MAX_LENGTH, Link

__all__ = ["LinkCreate", "LinkPatch", "LinkRead", "PublicLinkRead"]

_VALUE = (
    "The handle, as typed after the platform's prefix (`ada` for X, `ada.l` for "
    "Instagram), or a full link on the platform's own site; for a website, its "
    "address. Stored and returned in one canonical form; anything else is a `422` "
    "at `/value`."
)


class LinkCreate(Normalised):
    """A new link. One per platform, except `website` (up to three)."""

    model_config = ConfigDict(extra="forbid")

    platform: LinkPlatform = Field(
        description="LinkedIn is not here: it stays `social_linkedin` on the profile."
    )
    value: str = Field(max_length=MAX_LENGTH, description=_VALUE)


class LinkPatch(Normalised):
    """A new value for one link. The platform is fixed once the link exists."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(max_length=MAX_LENGTH, description=_VALUE)


class LinkRead(BaseModel):
    """One of your links, as the settings page edits it."""

    id: UUID
    platform: LinkPlatform
    value: str = Field(description="What follows the platform's prefix in `url`.")
    url: str = Field(description="The full canonical `https://` link. Render it; never parse it.")
    position: int = Field(description="Your order. Links come back sorted by it.")

    @classmethod
    def of(cls, link: Link) -> Self:
        return cls(
            id=link.id,
            platform=LinkPlatform(link.platform),
            value=link.value,
            url=link.url,
            position=link.position,
        )


class PublicLinkRead(BaseModel):
    """A link as a mentor's public profile shows it."""

    platform: LinkPlatform
    url: str = Field(description="The full canonical `https://` link. Render it; never parse it.")

    @classmethod
    def of(cls, link: Link) -> Self:
        return cls(platform=LinkPlatform(link.platform), url=link.url)

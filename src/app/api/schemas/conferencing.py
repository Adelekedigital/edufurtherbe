"""A mentor's default video provider, as they read and set it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import ConferencingProvider
from app.domain.meetings import MAX_MEETING_LINK_LENGTH, PLATFORM_DEFAULT_PROVIDER, meeting_link

__all__ = ["ConferencingRead", "ConferencingWrite"]


class ConferencingRead(BaseModel):
    """Where this mentor's sessions are held unless an offering says otherwise."""

    provider: ConferencingProvider = Field(
        description=(
            "`daily` is EduFurther video, `google_meet` a Meet link made on the "
            "platform's calendar, `custom` the mentor's own room link."
        )
    )
    #: The mentor's own room link. Theirs to read; never on a public read (#108).
    custom_url: str | None = Field(
        default=None, description="The personal room link, for `custom` only."
    )
    is_default_choice: bool = Field(
        description=(
            "True while the mentor has never chosen and gets the platform's "
            "default, EduFurther video."
        )
    )

    @classmethod
    def of(cls, saved: dict[str, Any] | None) -> ConferencingRead:
        if saved is None:
            return cls(provider=PLATFORM_DEFAULT_PROVIDER, custom_url=None, is_default_choice=True)
        return cls(
            provider=ConferencingProvider(str(saved["provider"])),
            custom_url=saved["custom_url"],
            is_default_choice=False,
        )


class ConferencingWrite(BaseModel):
    """A new default. `custom_url` is required for `custom` and refused otherwise,
    the same rule the table's CHECK holds."""

    model_config = ConfigDict(extra="forbid")

    provider: ConferencingProvider
    custom_url: str | None = Field(default=None, max_length=MAX_MEETING_LINK_LENGTH)

    @model_validator(mode="after")
    def _link_matches_provider(self) -> ConferencingWrite:
        if self.provider is ConferencingProvider.CUSTOM:
            if self.custom_url is None:
                raise ValueError("a personal link needs custom_url")
            self.custom_url = meeting_link(self.custom_url)
        elif self.custom_url is not None:
            raise ValueError("custom_url is only for a personal link (provider custom)")
        return self

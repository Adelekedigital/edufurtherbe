"""Every piece of configuration enters the process here.

No module outside this one reads ``os.environ``. Secrets are ``SecretStr`` so an
accidental log line or repr renders ``**********`` instead of the value.
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "EDUFURTHER_"

Environment = Literal["local", "ci", "staging", "production"]


class Settings(BaseSettings):
    """Process configuration, read from the environment and an optional ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix=ENV_PREFIX,
        extra="forbid",
    )

    environment: Environment = "local"
    debug: bool = False
    cors_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_unknown_prefixed_variables(self) -> "Settings":
        """Fail startup on a misspelled ``EDUFURTHER_`` variable.

        ``extra="forbid"`` does not cover this. pydantic-settings only collects
        environment variables that match a declared field, so an unmatched one is
        dropped before validation ever sees it — meaning a typo'd secret silently
        leaves the default in place. That is the failure mode where the process
        starts, looks healthy, and is misconfigured.
        """
        known = {f"{ENV_PREFIX}{name}".upper() for name in type(self).model_fields}
        unknown = sorted(
            name
            for name in os.environ
            if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
        )
        if unknown:
            raise ValueError(
                f"Unrecognised {ENV_PREFIX} variable(s): {', '.join(unknown)}. "
                "Declare the field on Settings, or correct the spelling."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed once."""
    return Settings()

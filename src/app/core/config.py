"""Every piece of configuration enters the process here.

No module outside this one reads ``os.environ``. Secrets are ``SecretStr`` so an
accidental log line or repr renders ``**********`` instead of the value.
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    # Optional, and deliberately so. ``main.py`` builds the application at import
    # time (``app = create_app()``), so a required field would make
    # ``import app.main`` fail on any machine without a database configured —
    # including every unit test and the pre-commit hook that runs them. The
    # engine factory raises ``ConfigurationError`` at the point of use instead,
    # which is where a missing DSN is actually actionable.
    #
    # ``SecretStr`` because a DSN carries a password: ``repr`` and any log line
    # render it as ``**********``. Read the value with ``.get_secret_value()``,
    # and only inside ``infra/``.
    database_url: SecretStr | None = None

    # The Bubble Data API, used to extract the legacy snapshot. Both optional for
    # the same reason as ``database_url``: the application imports on machines
    # that will never run an extract, and the script raises at the point of use.
    #
    # The URL carries the environment in its path — ``/version-test/`` is the dev
    # app and its absence is production — so it is configuration rather than a
    # constant. Trailing slashes are stripped by the adapter.
    bubble_api_url: str | None = None
    bubble_api_token: SecretStr | None = None

    @model_validator(mode="after")
    def reject_unknown_prefixed_variables(self) -> Settings:
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

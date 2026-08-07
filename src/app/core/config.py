"""Every piece of configuration enters the process here.

No module outside this one reads ``os.environ``. Secrets are ``SecretStr`` so an
accidental log line or repr renders ``**********`` instead of the value.
"""

import json
import os
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    # ``NoDecode`` is load-bearing, not decoration. Without it pydantic-settings
    # JSON-decodes a complex type *inside the settings source*, before any
    # validator runs, and a bare origin typed into a cloud console fails as
    # ``SettingsError: error parsing value for field "cors_origins"`` — a message
    # naming neither the value nor the expected shape, from a field that at the
    # time was wired to nothing. That took a deploy to diagnose.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def accept_json_or_a_comma_separated_list(cls, value: object) -> object:
        """Take either spelling, because both have a legitimate author.

        JSON is what ``.env.example`` documents and what any existing
        configuration already uses; comma-separated is what a person types into a
        form field that gives no hint a list is wanted. Supporting one and not the
        other breaks somebody, and the breakage is a service that will not start.

        Anything that is not a string — a real list from code, as the tests and
        ``create_app`` pass — is handed straight on to normal field validation.
        Disabling the decoder must not also disable the type check.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            # Let a malformed array raise here: inside a validator it becomes a
            # ``ValidationError`` naming the field, rather than the opaque
            # ``SettingsError`` this whole annotation exists to avoid.
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]

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

    # Supabase. Optional for the same reason as the others: importing the
    # application must not require credentials that only some code paths need,
    # and the point of use raises `ConfigurationError` with something actionable.
    #
    # Projects differ on signing: newer ones sign asymmetrically and publish a
    # JWKS, older ones use a shared HS256 secret. Set whichever the dashboard
    # shows; the verifier prefers the JWKS when both are present.
    supabase_url: str | None = None
    supabase_jwks_url: str | None = None
    supabase_jwt_secret: SecretStr | None = None
    # Bypasses RLS and can create or delete users. Never the anon key, never sent
    # to a browser, and used only by the provisioning CLI.
    supabase_service_role_key: SecretStr | None = None

    # Where re-hosted profile images live. A plain name, not a secret: it appears
    # in every public image URL. The bucket is created once by an operator in the
    # dashboard, not by the migration script — see `infra/storage/supabase.py`.
    supabase_storage_bucket: str = "profile-images"

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

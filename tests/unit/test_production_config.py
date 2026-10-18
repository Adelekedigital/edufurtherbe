"""Production refuses to start half-configured (`production_gaps`, `create_app`)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sentry_sdk
from pydantic import SecretStr

from app.core.config import PRODUCTION_REQUIRED, Settings, env_key, production_gaps
from app.core.errors import ConfigurationError
from app.main import create_app

PROJECT = "https://abcdefghijklmnopqrst.supabase.co"


@pytest.fixture(autouse=True)
def no_process_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`_env_file=None` still reads the process environment; a developer's
    exported `DATABASE_URL` must not fill a gap these tests expect. And a
    complete configuration starts Sentry, which is switched off again after."""
    for name in Settings.model_fields:
        monkeypatch.delenv(env_key(name), raising=False)
    yield
    sentry_sdk.get_client().close()
    sentry_sdk.init()


def complete(**overrides: object) -> Settings:
    """Settings carrying every value production needs."""
    values: dict[str, object] = {name: SecretStr("set") for name in PRODUCTION_REQUIRED}
    values |= {
        "database_url": SecretStr("postgresql://u:p@localhost:5432/db"),
        "daily_webhook_secret": SecretStr("MDEyMzQ1Njc4OWFiY2RlZg=="),
        "sentry_dsn": SecretStr("https://public@o0.ingest.sentry.io/1"),
        "supabase_url": PROJECT,
        "public_base_url": "https://api.example.test",
        "app_base_url": "https://app.example.test",
        "google_oauth_client_id": "client",
        "google_calendar_client_id": "client",
        "supabase_intake_bucket": "intake",
        "supabase_jwt_secret": SecretStr("secret"),
        "cors_origins": ["https://app.example.test"],
        "environment": "production",
    }
    return Settings(_env_file=None, **(values | overrides))


def test_a_complete_production_configuration_has_no_gaps_and_starts() -> None:
    settings = complete()

    assert production_gaps(settings) == []
    assert create_app(settings).state.settings is settings


@pytest.mark.parametrize("name", PRODUCTION_REQUIRED)
def test_each_required_value_is_named_when_missing(name: str) -> None:
    assert production_gaps(complete(**{name: None})) == [f"{env_key(name)} is not set"]


def test_an_empty_production_names_everything_it_needs() -> None:
    """Spelled out, not derived from `PRODUCTION_REQUIRED`: dropping a name from
    that tuple must fail here, not shrink the test with it."""
    settings = Settings(_env_file=None, environment="production")

    assert production_gaps(settings) == [
        "DATABASE_URL is not set",
        "SUPABASE_URL is not set",
        "LOOPS_API_KEY is not set",
        "DAILY_API_KEY is not set",
        "DAILY_WEBHOOK_SECRET is not set",
        "QSTASH_TOKEN is not set",
        "QSTASH_CURRENT_SIGNING_KEY is not set",
        "PUBLIC_BASE_URL is not set",
        "APP_BASE_URL is not set",
        "GOOGLE_OAUTH_CLIENT_ID is not set",
        "GOOGLE_OAUTH_CLIENT_SECRET is not set",
        "GOOGLE_CALENDAR_REFRESH_TOKEN is not set",
        "GOOGLE_CALENDAR_CLIENT_ID is not set",
        "GOOGLE_CALENDAR_CLIENT_SECRET is not set",
        "CALENDAR_TOKEN_KEY is not set",
        "SUPABASE_INTAKE_BUCKET is not set",
        "SENTRY_DSN is not set",
        "neither SUPABASE_JWKS_URL nor SUPABASE_JWT_SECRET is set",
        "CORS_ORIGINS is empty",
    ]


def test_a_blank_value_counts_as_missing() -> None:
    """A console clears a variable to an empty string, not to nothing."""
    gaps = production_gaps(complete(loops_api_key=SecretStr("  "), public_base_url=" "))

    assert gaps == ["LOOPS_API_KEY is not set", "PUBLIC_BASE_URL is not set"]


def test_a_token_key_cors_and_debug_are_required_too() -> None:
    gaps = production_gaps(complete(supabase_jwt_secret=None, cors_origins=[], debug=True))

    assert gaps == [
        "neither SUPABASE_JWKS_URL nor SUPABASE_JWT_SECRET is set",
        "CORS_ORIGINS is empty",
        "EDUFURTHER_DEBUG is on",
    ]


def test_a_jwks_url_alone_is_enough_to_verify_tokens() -> None:
    settings = complete(
        supabase_jwt_secret=None, supabase_jwks_url=f"{PROJECT}/auth/v1/.well-known/jwks.json"
    )

    assert production_gaps(settings) == []


def test_production_refuses_to_start_and_names_every_gap_at_once() -> None:
    with pytest.raises(ConfigurationError) as refused:
        create_app(complete(daily_api_key=None, qstash_token=None))

    assert "DAILY_API_KEY is not set" in str(refused.value)
    assert "QSTASH_TOKEN is not set" in str(refused.value)


@pytest.mark.parametrize("environment", ["local", "ci", "staging"])
def test_other_environments_start_with_nothing_configured(environment: str) -> None:
    """Only the web app in production is held to this: tests, local runs and the
    staging deployment keep their null adapters."""
    settings = Settings(_env_file=None, environment=environment)

    assert production_gaps(settings)
    assert create_app(settings).state.settings is settings


def test_settings_themselves_never_refuse_production() -> None:
    """Migrations and scripts build `Settings` with only a DSN; the check
    living in a validator would stop `alembic upgrade` in production."""
    settings = Settings(_env_file=None, environment="production")

    assert settings.environment == "production"


def test_the_writing_assistant_needs_nothing_while_it_is_off() -> None:
    """Off by default (decision 267): production starts without router settings."""
    assert production_gaps(complete(writing_assist_enabled=False)) == []


def test_switched_on_it_names_the_router_settings_it_lacks() -> None:
    gaps = production_gaps(complete(writing_assist_enabled=True))

    assert gaps == ["AI_ROUTER_URL is not set", "AI_ROUTER_PRIVATE_KEY is not set"]


def test_switched_on_and_configured_it_has_no_gaps() -> None:
    configured = complete(
        writing_assist_enabled=True,
        ai_router_url="https://router.example.test",
        ai_router_private_key=SecretStr("set"),
    )

    assert production_gaps(configured) == []

"""Configuration behaviour.

The fail-fast case matters most: a typo'd variable must stop the process rather
than silently leave a default in place.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, get_settings


def test_settings_default_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDUFURTHER_ENVIRONMENT", raising=False)
    monkeypatch.delenv("EDUFURTHER_DEBUG", raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.debug is False
    assert settings.cors_origins == []


def test_settings_read_from_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDUFURTHER_ENVIRONMENT", "staging")
    monkeypatch.setenv("EDUFURTHER_DEBUG", "true")

    settings = Settings(_env_file=None)

    assert settings.environment == "staging"
    assert settings.debug is True


def test_unknown_prefixed_variable_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDUFURTHER_DATABSE_URL", "postgresql://localhost/x")

    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None)


def test_get_settings_returns_the_same_instance() -> None:
    assert get_settings() is get_settings()

"""Configuration behaviour.

The fail-fast case matters most: a typo'd variable must stop the process rather
than silently leave a default in place.
"""

from pathlib import Path

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
    """``DATABSE`` is misspelled on purpose, and the misspelling is load-bearing.

    ``EDUFURTHER_DATABASE_URL`` is now a real field, so correcting the typo would
    turn this into a test that a *valid* variable is accepted — the opposite
    assertion, passing for the wrong reason. Change the probe only to another
    variable that does not exist.
    """
    monkeypatch.setenv("EDUFURTHER_DATABSE_URL", "postgresql://localhost/x")

    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None)


def test_database_url_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDUFURTHER_DATABASE_URL", raising=False)

    assert Settings(_env_file=None).database_url is None


def test_database_url_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDUFURTHER_DATABASE_URL", "postgresql://u:pw@localhost:5432/db")

    settings = Settings(_env_file=None)

    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == "postgresql://u:pw@localhost:5432/db"


def test_database_url_is_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSN carries a password, and ``repr`` is what ends up in a traceback.

    Probes the password specifically rather than asserting the mask is present:
    a plain ``str`` field would also render *something*, and only the absence of
    the secret distinguishes the two.
    """
    monkeypatch.setenv("EDUFURTHER_DATABASE_URL", "postgresql://u:sup3rs3cret@localhost/db")

    rendered = repr(Settings(_env_file=None))

    assert "sup3rs3cret" not in rendered
    assert "**********" in rendered


def test_get_settings_returns_the_same_instance() -> None:
    assert get_settings() is get_settings()


def test_settings_fixture_ignores_a_dotenv_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """The shared fixture must not read whatever ``.env`` the developer has.

    Resolved lazily, after the working directory moves, so the fixture is built
    in the presence of the file it is supposed to ignore.

    ``cors_origins`` is the probe deliberately: the fixture pins ``environment``
    and ``debug`` as init arguments, and those outrank a dotenv value whether or
    not the file is read. Only a field the fixture leaves alone can show the leak.
    """
    (tmp_path / ".env").write_text(
        'EDUFURTHER_CORS_ORIGINS=["https://leaked.example"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EDUFURTHER_CORS_ORIGINS", raising=False)

    settings: Settings = request.getfixturevalue("settings")

    assert settings.cors_origins == []
    assert settings.environment == "ci"

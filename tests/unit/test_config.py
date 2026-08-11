"""Configuration behaviour.

The fail-fast case matters most: a typo'd variable must stop the process rather
than silently leave a default in place.
"""

from pathlib import Path

import pytest
from pydantic import SecretStr
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
    """A misspelled ``EDUFURTHER_`` variable must stop the process.

    The probe must name something that is not a field under *either* spelling —
    a real field's old name is caught by the migration guard below instead, which
    is a different message and a different reason.
    """
    monkeypatch.setenv("EDUFURTHER_NOT_A_FIELD_AT_ALL", "x")

    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None)


# These are the OLD keys, on purpose, and a bulk rename must never touch them —
# the whole point is that they no longer work.
@pytest.mark.parametrize(
    "stale",
    ["EDUFURTHER_SUPABASE_URL", "EDUFURTHER_DATABASE_URL", "EDUFURTHER_CORS_ORIGINS"],
)
def test_an_old_prefixed_key_stops_the_process(stale: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """**The test this change exists for.**

    These keys were correct until the prefix was dropped. A deployed environment
    still holding one would otherwise start *healthy with nothing configured* —
    the exact silent-misconfiguration failure the prefix guard was written to
    prevent, reintroduced by removing the prefix.

    Failing loudly is the whole design: the operator gets a message naming both
    the old key and the new one, rather than a service that runs and cannot reach
    Supabase.

    **The assertion is the rename arrow, not the key names.** These keys begin
    with ``EDUFURTHER_``, so the *unknown-key* branch would also reject them — and
    with a message that says which keys carry the prefix but never says what to
    rename this one to. Asserting the names alone passed against both branches,
    which a mutation proved by deleting this guard entirely and leaving the suite
    green. Worse, ``"SUPABASE_URL" in "EDUFURTHER_SUPABASE_URL"`` is true, so the
    second assertion was satisfied by substring coincidence.

    Only the arrow distinguishes the actionable message from the generic one.
    """
    monkeypatch.setenv(stale, "anything")

    with pytest.raises(PydanticValidationError) as raised:
        Settings(_env_file=None)

    message = str(raised.value)
    assert f"{stale} -> {stale.removeprefix('EDUFURTHER_')}" in message, message


def test_the_two_collision_prone_keys_keep_their_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ENVIRONMENT`` and ``DEBUG`` are generic enough to be set by something
    else on the host, so they alone keep the prefix. The unprefixed spelling must
    *not* be read, or the rule is decorative."""
    monkeypatch.setenv("EDUFURTHER_ENVIRONMENT", "staging")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEBUG", "true")

    settings = Settings(_env_file=None)

    assert settings.environment == "staging"
    assert settings.debug is False


def test_the_unprefixed_keys_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@localhost:5432/db")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example")

    settings = Settings(_env_file=None)

    assert settings.supabase_url == "https://project.supabase.co"
    assert settings.database_url is not None
    assert settings.cors_origins == ["https://app.example"]


def test_database_url_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert Settings(_env_file=None).database_url is None


def test_database_url_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@localhost:5432/db")

    settings = Settings(_env_file=None)

    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == "postgresql://u:pw@localhost:5432/db"


def test_database_url_is_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSN carries a password, and ``repr`` is what ends up in a traceback.

    Probes the password specifically rather than asserting the mask is present:
    a plain ``str`` field would also render *something*, and only the absence of
    the secret distinguishes the two.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:sup3rs3cret@localhost/db")

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
    (tmp_path / ".env").write_text('CORS_ORIGINS=["https://leaked.example"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    settings: Settings = request.getfixturevalue("settings")

    assert settings.cors_origins == []
    assert settings.environment == "ci"


# --------------------------------------------------------------------------
# The Supabase values name one project, or the process refuses to start
# --------------------------------------------------------------------------

POOLER = (
    "postgresql://postgres.abcdefghijklmnop:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
)
OTHER_POOLER = (
    "postgresql://postgres.zyxwvutsrqponmlk:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
)
LOCAL = "postgresql://edufurther:edufurther@localhost:55432/edufurther"


def test_supabase_values_naming_one_project_are_accepted() -> None:
    """The accepting case, beside the refusing one below.

    A guard that refused everything would satisfy the mismatch test on its own,
    so this is what stops the fix being "raise unconditionally".
    """
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(POOLER),
        supabase_url="https://abcdefghijklmnop.supabase.co",
    )

    assert settings.database_url is not None


def test_a_database_and_a_supabase_url_naming_different_projects_are_refused() -> None:
    """**The failure this exists for, and it has no undo.**

    Five values name a Supabase project and nothing tied them together. Point the
    DSN at staging while `SUPABASE_URL` still names production and
    `provision_auth.py --link-migrated` reads users out of one project's database
    and creates real auth accounts in another's — reporting success, because from
    the loader's view nothing went wrong.

    Recorded in `failure-modes.md` while the credentials to do it existed
    nowhere. They exist now.
    """
    with pytest.raises(PydanticValidationError) as raised:
        Settings(
            _env_file=None,
            database_url=SecretStr(POOLER),
            supabase_url="https://zyxwvutsrqponmlk.supabase.co",
        )

    message = str(raised.value)
    assert "abcdefghijklmnop" in message and "zyxwvutsrqponmlk" in message, message
    assert "pw@" not in message, "the refusal must not carry the password"


def test_a_local_database_beside_a_supabase_url_is_left_alone() -> None:
    """**The normal development state, and the reason the guard is narrow.**

    A `localhost` DSN next to a real `SUPABASE_URL` is what every developer here
    runs: the database is Docker, auth is the shared project. It names no Supabase
    project, so there is nothing to disagree with.

    Widening the guard to refuse this would have made the suite fail and invited
    the fix that loosens it until green — a guard that guards nothing.
    """
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(LOCAL),
        supabase_url="https://abcdefghijklmnop.supabase.co",
    )

    assert settings.database_url is not None


def test_a_direct_connection_host_is_read_for_its_project_too() -> None:
    """Both DSN shapes carry the ref, in different places — `postgres.<ref>` on
    the pooler and `db.<ref>.supabase.co` on the direct connection. Reading only
    one would leave the other unguarded, and the direct form is what a developer
    copies from the dashboard first."""
    with pytest.raises(PydanticValidationError):
        Settings(
            _env_file=None,
            database_url=SecretStr(
                "postgresql://postgres:pw@db.abcdefghijklmnop.supabase.co:5432/postgres"
            ),
            supabase_url="https://zyxwvutsrqponmlk.supabase.co",
        )


def test_values_that_are_absent_cannot_disagree() -> None:
    """Nothing to compare is not a mismatch. Most processes here set only the
    DSN, and refusing them would make the guard unusable."""
    assert Settings(_env_file=None, database_url=SecretStr(POOLER)).database_url is not None
    assert Settings(_env_file=None, supabase_url="https://abcdefghijklmnop.supabase.co") is not None


def test_the_env_file_is_selectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ENV_FILE`` chooses which dotenv is read, so switching environment is one
    file rather than five variables edited by hand — which is the shape the
    mismatch above actually takes.

    Read at import, which pairs with ``get_settings`` being cached: one process,
    one file, no switching mid-run.
    """
    (tmp_path / ".env.staging").write_text(
        "CORS_ORIGINS=https://staging.example\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    settings = Settings(_env_file=".env.staging")

    assert settings.cors_origins == ["https://staging.example"]

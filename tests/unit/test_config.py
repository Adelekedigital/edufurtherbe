"""Configuration behaviour.

The fail-fast case matters most: a typo'd variable must stop the process rather
than silently leave a default in place.
"""

import base64
import re
from pathlib import Path

import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, env_key, get_settings
from app.domain.attendance import JOIN_LEAD_CEILING
from app.domain.sessions import CANCELLATION_CUTOFF


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
    monkeypatch.setenv("REQUIRE_INTAKE_ANSWERS", "true")
    monkeypatch.setenv("MAX_BOOKING_WINDOW_DAYS", "90")
    monkeypatch.setenv("DEFAULT_BOOKING_WINDOW_DAYS", "30")

    settings = Settings(_env_file=None)

    assert settings.supabase_url == "https://project.supabase.co"
    assert settings.database_url is not None
    assert settings.cors_origins == ["https://app.example"]
    assert settings.require_intake_answers is True
    assert (settings.max_booking_window_days, settings.default_booking_window_days) == (90, 30)


def test_a_default_window_above_the_max_is_refused() -> None:
    with pytest.raises(ValueError, match="DEFAULT_BOOKING_WINDOW_DAYS"):
        Settings(_env_file=None, max_booking_window_days=14, default_booking_window_days=30)


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


@pytest.mark.parametrize("value", ["app.example.com", "/app", "ftp://app.example.com"])
def test_an_app_origin_without_a_web_scheme_is_refused(value: str) -> None:
    """Codex on #340: every email link is joined to this, so a value with no
    scheme sends links no mail client can open. Refused at start-up instead."""
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, app_base_url=value)


def test_the_join_window_opens_ten_minutes_early_unless_configured() -> None:
    """Owner, 2026-10-08: ten minutes before the start, as a setting."""
    assert Settings(_env_file=None).join_window_opens_minutes == 10


CEILING_MINUTES = int(JOIN_LEAD_CEILING.total_seconds() // 60)


@pytest.mark.parametrize("value", [-1, CEILING_MINUTES + 1, 60])
def test_a_join_lead_past_the_cancellation_cutoff_is_refused(value: int) -> None:
    """**Capped at the cancellation cutoff** (Codex on #391). A party may cancel
    until ten minutes before the start, so a window opening earlier would let
    one party be marked present and enter while the other could still cancel
    and release the session."""
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, join_window_opens_minutes=value)


def test_the_join_lead_ceiling_is_the_cancellation_cutoff() -> None:
    """**One rule, pinned across a layer the config cannot import.** The setting's
    bound is a literal in `core/config.py`, which may not import the domain, so
    this fails the moment the bound and the cutoff diverge. The ceiling itself
    is accepted."""
    assert JOIN_LEAD_CEILING == CANCELLATION_CUTOFF
    assert Settings(_env_file=None, join_window_opens_minutes=CEILING_MINUTES)


@pytest.mark.parametrize(
    "value",
    [
        "https://app.example.test?tenant=a",
        "https://app.example.test/?tenant=a",
        "https://app.example.test#top",
        "https://",
    ],
)
def test_an_app_origin_with_a_query_fragment_or_no_host_is_refused(value: str) -> None:
    """Codex on #390: every link is the origin with a path *appended*, so a query
    or fragment swallows the path — `?tenant=a/sessions/{id}` opens the home
    page — and no host is no link at all. Refused at start-up, for the emails
    and the calendar invite alike."""
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, app_base_url=value)


@pytest.mark.parametrize(
    "value",
    [
        "https://app.edufurther.org",
        "http://localhost:3000",
        "https://app.edufurther.org/",
        "https://example.test/app",
    ],
)
def test_an_app_origin_with_a_web_scheme_is_kept(value: str) -> None:
    assert Settings(_env_file=None, app_base_url=value).app_base_url == value


# --------------------------------------------------------------------------
# `.env.example` is the only place an operator finds out a variable exists.
#
# Nothing reads it, so nothing fails when a new setting skips it — the cost
# lands later, on whoever configures a deployment from it and gets a default
# they never chose. Five settings had drifted out of it before this test was
# written. Non-negotiable #8: two representations of the same set.
# --------------------------------------------------------------------------

#: Documented on purpose while not being a `Settings` field: read by
#: `scripts/supabase_otp.py` and by nothing in the application, which the file
#: says where it is defined.
NOT_SETTINGS_FIELDS = frozenset({"SUPABASE_ANON_KEY"})


def _documented() -> set[str]:
    text = Path(".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


def test_every_setting_is_documented_in_env_example() -> None:
    """Add a field without documenting it and this fails, naming it."""
    missing = sorted({env_key(name) for name in Settings.model_fields} - _documented())
    assert not missing, f"undocumented in .env.example: {missing}"


def test_env_example_documents_nothing_that_is_not_a_setting() -> None:
    """The other direction: a variable removed from `Settings` and left behind.

    An operator cannot tell a live setting from a dead one, so a stale line is
    worse than a missing one — it will be set, and nothing will read it.
    """
    keys = {env_key(name) for name in Settings.model_fields}
    stale = sorted(_documented() - keys - NOT_SETTINGS_FIELDS)
    assert not stale, f"in .env.example but not a Settings field: {stale}"


#: Stand-ins for Daily's webhook secret. Named rather than inline because the
#: secret scanner flags a literal in that position, correctly.
NOT_BASE64_SECRET = "not base64 %%%"  # noqa: S105
#: Built at runtime, so no secret-shaped literal lands in the repository.
BASE64_SECRET = base64.b64encode(b"thirty-two-bytes-of-test-secret!").decode()
SHORT_SECRET = "c2hvcnQ="  # noqa: S105 - "short", five bytes


@pytest.mark.parametrize("value", [pytest.param("", id="empty"), pytest.param("   ", id="blank")])
def test_an_empty_daily_webhook_secret_counts_as_not_configured(value: str) -> None:
    """**An empty key is a key anyone holds** (security review, #382). Blank is
    how `.env.example` ships it and how a Railway variable starts, and an HMAC
    under an empty key is one any caller can compute. So blank means *not
    configured*, which refuses every delivery, rather than a key."""
    assert Settings(_env_file=None, daily_webhook_secret=value).daily_webhook_secret is None


def test_a_daily_webhook_secret_under_sixteen_bytes_is_refused() -> None:
    """Short enough to guess is short enough to forge with: refused at start-up."""
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, daily_webhook_secret=SHORT_SECRET)


def test_a_daily_webhook_secret_that_is_not_base64_is_refused() -> None:
    """It would otherwise refuse every delivery at runtime as if forged (#382)."""
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, daily_webhook_secret=NOT_BASE64_SECRET)


def test_a_base64_daily_webhook_secret_is_kept() -> None:
    settings = Settings(_env_file=None, daily_webhook_secret=BASE64_SECRET)
    assert settings.daily_webhook_secret is not None
    assert settings.daily_webhook_secret.get_secret_value() == BASE64_SECRET


def test_the_mentee_refund_window_defaults_to_twelve_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset, the rule is unchanged: twelve hours (decision 229)."""
    monkeypatch.delenv("MENTEE_CANCEL_REFUND_HOURS", raising=False)

    assert Settings(_env_file=None).mentee_cancel_refund_hours == 12  # type: ignore[call-arg]


def test_the_mentee_refund_window_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MENTEE_CANCEL_REFUND_HOURS", "6")

    assert Settings(_env_file=None).mentee_cancel_refund_hours == 6  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["0", "-1", "twelve", "1.5", "8761", "120000000"])
def test_a_refund_window_that_is_not_a_positive_whole_hour_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """**A typo fails the boot**, rather than refunding everyone (0), nobody, or
    crashing every session read on an overflow (a slipped run of zeros)."""
    monkeypatch.setenv("MENTEE_CANCEL_REFUND_HOURS", value)

    with pytest.raises(PydanticValidationError, match="MENTEE_CANCEL_REFUND_HOURS"):
        Settings(_env_file=None)  # type: ignore[call-arg]

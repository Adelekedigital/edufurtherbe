"""The two writing-assist migrations (Onboarding v5 B7 and #8, decision 266).

`f1a6c3e9b274` records where a bio came from: `user_profiles.about_me_drafted_at`
and `about_me_prompt_version`, both or neither. `a8d2e5c7f391` adds
`writing_assist_requests`, the rows the per-user daily limit counts.

Its own file because `test_migrations.py` is at the size limit (rule 11). Every
constraint has an accepting and a rejecting case: a test that only proves a
constraint refuses garbage cannot tell it from one that refuses everything.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.profile_text import PROMPT_VERSION_MAX_LENGTH, PROMPT_VERSION_PATTERN

pytestmark = pytest.mark.db

PROVENANCE = "f1a6c3e9b274"
REQUESTS = "a8d2e5c7f391"
BEFORE = "e4710b359d1b"

_PROFILE = (
    "WITH u AS (INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentee', 'UTC') RETURNING id) "
    "INSERT INTO user_profiles (user_id, about_me, about_me_drafted_at, about_me_prompt_version) "
    "SELECT id, 'A bio.', {drafted}, {version} FROM u"
)

_USER = (
    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentee', 'UTC')"
)

_REQUEST = (
    "INSERT INTO writing_assist_requests (user_id, field) "
    "SELECT id, {field} FROM users WHERE email = '{email}'"
)


def _profile(url: str, email: str, drafted: str, version: str) -> None:
    execute(url, _PROFILE.format(email=email, drafted=drafted, version=version))


@pytest.mark.parametrize(
    ("drafted", "version"),
    [
        ("NULL", "NULL"),
        ("now()", "'bio_draft-v1'"),
        ("now()", "'bio_improve-v12'"),
        # The longest a version may be: 64 characters.
        ("now()", "'b' || repeat('x', 60) || '-v1'"),
    ],
    ids=["neither", "draft", "improve", "sixty-four"],
)
def test_a_profile_holds_provenance_both_or_neither(
    disposable_database: str, make_alembic_config: ConfigFactory, drafted: str, version: str
) -> None:
    command.upgrade(make_alembic_config(disposable_database), PROVENANCE)
    _profile(disposable_database, "ok@example.test", drafted, version)

    assert scalar(disposable_database, "SELECT count(*) FROM user_profiles") == 1


@pytest.mark.parametrize(
    ("drafted", "version"),
    [
        ("now()", "NULL"),
        ("NULL", "'bio_draft-v1'"),
    ],
    ids=["stamp-only", "version-only"],
)
def test_half_a_provenance_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory, drafted: str, version: str
) -> None:
    command.upgrade(make_alembic_config(disposable_database), PROVENANCE)

    with pytest.raises(asyncpg.CheckViolationError, match="about_me_provenance_both_or_neither"):
        _profile(disposable_database, "half@example.test", drafted, version)


@pytest.mark.parametrize(
    "version",
    [
        "'Bio_draft-v1'",
        "'bio_draft'",
        "'bio_draft-v'",
        "'bio_draft-vx'",
        "'-v1'",
        "'1bio-v1'",
        "'bio draft-v1'",
        "'bio_draft-v1 '",
        "'bio-draft-v1'",
        "''",
        # One over the limit: 65 characters.
        "'b' || repeat('x', 61) || '-v1'",
    ],
    ids=[
        "uppercase",
        "no-version",
        "no-number",
        "letter-number",
        "no-task",
        "digit-first",
        "space",
        "trailing-space",
        "hyphen-in-task",
        "blank",
        "sixty-five",
    ],
)
def test_a_malformed_prompt_version_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory, version: str
) -> None:
    command.upgrade(make_alembic_config(disposable_database), PROVENANCE)

    with pytest.raises(asyncpg.CheckViolationError, match="about_me_prompt_version_shape"):
        _profile(disposable_database, "bad@example.test", "now()", version)


def test_provenance_without_a_bio_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Provenance describes stored text, so a bio cleared under it is refused;
    clearing all three together is accepted."""
    command.upgrade(make_alembic_config(disposable_database), PROVENANCE)
    _profile(disposable_database, "cleared@example.test", "now()", "'bio_draft-v1'")

    with pytest.raises(asyncpg.CheckViolationError, match="about_me_provenance_has_a_bio"):
        execute(disposable_database, "UPDATE user_profiles SET about_me = NULL")
    execute(
        disposable_database,
        "UPDATE user_profiles SET about_me = NULL, about_me_drafted_at = NULL, "
        "about_me_prompt_version = NULL",
    )
    assert scalar(disposable_database, "SELECT count(*) FROM user_profiles") == 1


def test_the_version_shape_is_the_domains(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """One rule, one representation (non-negotiable 8): the CHECK the migration
    wrote carries the pattern and bound the domain and the model read, so a
    change to one without the other fails here."""
    command.upgrade(make_alembic_config(disposable_database), "head")
    definition = scalar(
        disposable_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_user_profiles_about_me_prompt_version_shape'",
    )

    assert f"'{PROMPT_VERSION_PATTERN}'" in definition, definition
    assert f"<= {PROMPT_VERSION_MAX_LENGTH})" in definition, definition


def test_the_provenance_downgrade_refuses_while_a_profile_holds_one(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Dropping the columns would delete the record that a bio was drafted, so a
    downgrade over values stops and says so; without values it drops both and
    comes back."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PROVENANCE)
    _profile(disposable_database, "drafted@example.test", "now()", "'bio_draft-v1'")

    with pytest.raises(RuntimeError, match="provenance"):
        command.downgrade(config, BEFORE)

    execute(
        disposable_database,
        "UPDATE user_profiles SET about_me_drafted_at = NULL, about_me_prompt_version = NULL",
    )
    command.downgrade(config, BEFORE)
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'user_profiles' "
            "AND column_name IN ('about_me_drafted_at', 'about_me_prompt_version')",
        )
        == 0
    )
    command.upgrade(config, PROVENANCE)


def test_a_request_names_a_known_field_and_gets_its_own_id(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    command.upgrade(make_alembic_config(disposable_database), REQUESTS)
    execute(disposable_database, _USER.format(email="asker@example.test"))
    for field in ("'mentee_bio'", "'mentor_about'"):
        execute(disposable_database, _REQUEST.format(field=field, email="asker@example.test"))

    assert (
        scalar(disposable_database, "SELECT count(DISTINCT id) FROM writing_assist_requests") == 2
    )
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM writing_assist_requests WHERE created_at IS NOT NULL",
        )
        == 2
    )


@pytest.mark.parametrize("field", ["'why_mentor'", "'Mentee_bio'", "''"])
def test_an_unknown_field_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory, field: str
) -> None:
    command.upgrade(make_alembic_config(disposable_database), REQUESTS)
    execute(disposable_database, _USER.format(email="asker@example.test"))

    with pytest.raises(asyncpg.CheckViolationError, match="field_is_known"):
        execute(disposable_database, _REQUEST.format(field=field, email="asker@example.test"))


def test_a_request_goes_with_its_user(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The rows exist only to count against a limit; nothing else reads them, so
    they cascade rather than hold up a user's deletion."""
    command.upgrade(make_alembic_config(disposable_database), REQUESTS)
    execute(disposable_database, _USER.format(email="gone@example.test"))
    execute(disposable_database, _USER.format(email="stays@example.test"))
    execute(disposable_database, _REQUEST.format(field="'mentee_bio'", email="gone@example.test"))
    execute(disposable_database, _REQUEST.format(field="'mentee_bio'", email="stays@example.test"))

    execute(disposable_database, "DELETE FROM users WHERE email = 'gone@example.test'")

    assert scalar(disposable_database, "SELECT count(*) FROM writing_assist_requests") == 1


def _plan(url: str, sql: str, *args: object) -> str:
    async def _run() -> str:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(f"EXPLAIN {sql}", *args)
            return "\n".join(row[0] for row in rows)
        finally:
            await conn.close()

    return asyncio.run(_run())


def test_counting_a_users_day_for_one_field_reads_the_index(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The limit's count is asserted with `EXPLAIN`, not assumed (guardrail).

    Two hundred users at fifty requests each is far past what ten a day per
    field reaches in practice, so a sequential scan here would be one at scale.
    """
    command.upgrade(make_alembic_config(disposable_database), REQUESTS)
    execute(
        disposable_database,
        "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
        "SELECT 'u' || n || '@example.test', gen_random_uuid(), 'Probe', 'mentee', 'UTC' "
        "FROM generate_series(1, 200) n; "
        "INSERT INTO writing_assist_requests (user_id, field, created_at) "
        "SELECT u.id, CASE WHEN n % 2 = 0 THEN 'mentee_bio' ELSE 'mentor_about' END, "
        "now() - n * interval '1 hour' FROM users u, generate_series(1, 50) n; "
        "ANALYZE writing_assist_requests",
    )
    user_id = scalar(disposable_database, "SELECT id FROM users WHERE email = 'u7@example.test'")
    plan = _plan(
        disposable_database,
        "SELECT count(*) FROM writing_assist_requests "
        "WHERE user_id = $1 "
        "AND field = 'mentee_bio' AND created_at > now() - interval '24 hours'",
        user_id,
    )

    assert "ix_writing_assist_requests_user_field_created" in plan, plan
    # The index narrows by the user and field, not by the time alone.
    assert f"Index Cond: ((user_id = '{user_id}'::uuid) AND (field = " in plan, plan
    assert "Seq Scan" not in plan, plan


def test_the_requests_downgrade_drops_the_table_and_comes_back(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Losing the rows only resets everybody's daily count, so the downgrade
    drops the table over rows rather than refusing."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REQUESTS)
    execute(disposable_database, _USER.format(email="asker@example.test"))
    execute(disposable_database, _REQUEST.format(field="'mentee_bio'", email="asker@example.test"))

    command.downgrade(config, PROVENANCE)
    assert scalar(disposable_database, "SELECT to_regclass('writing_assist_requests')") is None

    command.upgrade(config, REQUESTS)

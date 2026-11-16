"""What the profile-link tracking migration leaves behind (decision 346).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

pytestmark = pytest.mark.db

REVISION = "c569432b779f"
PREVIOUS = "c4d81f2a6e57"

MENTOR = "00000000-0000-7000-8000-000000000346"
#: One mentor, id written into the statements: every interpolation below is a
#: fixed test string, so S608 is suppressed where one is built.
SEED = (
    "INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES "  # noqa: S608
    f"('{MENTOR}', gen_random_uuid(), 'linked@example.test', 'mentor', 'UTC')"
)
KEY = "decode(repeat('ab', 32), 'hex')"


def visit(key: str = KEY, via: str = "NULL", host: str = "NULL", day: str = "2026-11-23") -> str:
    return (
        "INSERT INTO mentor_profile_visits "  # noqa: S608
        "(mentor_user_id, day, visitor_key, signed_in, via, referrer_host) "
        f"VALUES ('{MENTOR}', '{day}', {key}, false, {via}, {host})"
    )


def share(method: str = "'copy'", signed_in: str = "true", own: str = "true") -> str:
    return (
        "INSERT INTO mentor_profile_shares "  # noqa: S608
        "(mentor_user_id, day, visitor_key, signed_in, method, own) "
        f"VALUES ('{MENTOR}', '2026-11-23', {KEY}, {signed_in}, {method}, {own})"
    )


def test_one_visit_per_visitor_per_mentor_per_day(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, SEED)

    execute(disposable_database, visit(via="'copy'", host="'wa.me'"))
    assert scalar(
        disposable_database,
        "SELECT id IS NOT NULL AND created_at IS NOT NULL AND via = 'copy' "
        "AND referrer_host = 'wa.me' FROM mentor_profile_visits",
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, visit())
    execute(disposable_database, visit(day="2026-11-24"))
    execute(disposable_database, visit(key="decode(repeat('cd', 32), 'hex')"))
    assert scalar(disposable_database, "SELECT count(*) FROM mentor_profile_visits") == 3


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (visit(via="'email'"), "ck_mentor_profile_visits_via_is_known"),
        (visit(host="repeat('a', 254)"), "ck_mentor_profile_visits_referrer_host_fits"),
        (visit(host="''"), "ck_mentor_profile_visits_referrer_host_fits"),
        (
            visit(key="decode(repeat('ab', 31), 'hex')"),
            "ck_mentor_profile_visits_visitor_key_is_a_digest",
        ),
        (share(method="'email'"), "ck_mentor_profile_shares_method_is_known"),
        (share(signed_in="false"), "ck_mentor_profile_shares_own_is_signed_in"),
    ],
    ids=["via", "long-host", "empty-host", "short-key", "method", "own-guest"],
)
def test_the_checks_refuse_what_the_api_never_writes(
    disposable_database: str, migrate_to: MigrateTo, statement: str, constraint: str
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, SEED)

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        execute(disposable_database, statement)


def test_one_share_per_person_per_method_per_day_going_with_the_mentor(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, visit(host=f"'{'a' * 253}'"))

    execute(disposable_database, share())
    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, share())
    execute(disposable_database, share(method="'native'"))
    execute(
        disposable_database,
        share(method="'native'", signed_in="false", own="false").replace(
            "repeat('ab', 32)", "repeat('cd', 32)"
        ),
    )
    assert scalar(disposable_database, "SELECT count(*) FROM mentor_profile_shares") == 3

    execute(disposable_database, f"DELETE FROM users WHERE id = '{MENTOR}'")  # noqa: S608
    assert scalar(disposable_database, "SELECT count(*) FROM mentor_profile_shares") == 0
    assert scalar(disposable_database, "SELECT count(*) FROM mentor_profile_visits") == 0


def test_the_downgrade_drops_both_tables(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, visit())
    execute(disposable_database, share())

    command.downgrade(make_alembic_config(disposable_database), PREVIOUS)

    assert scalar(disposable_database, "SELECT to_regclass('mentor_profile_visits')") is None
    assert scalar(disposable_database, "SELECT to_regclass('mentor_profile_shares')") is None

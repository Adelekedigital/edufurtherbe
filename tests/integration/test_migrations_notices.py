"""What the `user_dismissed_notices` migration leaves behind (decision 356).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import asyncpg
import pytest
from tests.integration.test_migrations import MigrateTo, execute, scalar

from app.domain.seen_items import KEY_MAX_LENGTH

pytestmark = pytest.mark.db

REVISION = "db6fbb2aae43"

ALICE = "00000000-0000-7000-8000-000000000001"
BOB = "00000000-0000-7000-8000-000000000002"
KEY = "whats-new:2026-11-migration"

SEED_TEMPLATE = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('{alice}', gen_random_uuid(), 'a@example.test', 'mentee', 'UTC'),
  ('{bob}', gen_random_uuid(), 'b@example.test', 'mentor', 'UTC');
"""
SEED = SEED_TEMPLATE.format(alice=ALICE, bob=BOB)
DELETE_USER = "DELETE FROM users WHERE id = '{}'"
ROW = "INSERT INTO user_dismissed_notices (user_id, notice_key) VALUES ('{}', '{}')"
COUNT = "SELECT count(*) FROM user_dismissed_notices"


def upgraded(url: str, migrate_to: MigrateTo) -> None:
    migrate_to(url, REVISION)
    execute(url, SEED)


def test_a_notice_is_stored_once_per_user(disposable_database: str, migrate_to: MigrateTo) -> None:
    upgraded(disposable_database, migrate_to)

    execute(disposable_database, ROW.format(ALICE, KEY))
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM user_dismissed_notices "
            "WHERE id IS NOT NULL AND created_at IS NOT NULL",
        )
        == 1
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, ROW.format(ALICE, KEY))
    execute(disposable_database, ROW.format(BOB, KEY))
    assert scalar(disposable_database, COUNT) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Upper",
        "-dash-first",
        ":colon-first",
        "has space",
        "has/slash",
        "k" * (KEY_MAX_LENGTH + 1),
    ],
)
def test_the_database_refuses_a_key_of_the_wrong_shape(
    disposable_database: str, migrate_to: MigrateTo, bad: str
) -> None:
    upgraded(disposable_database, migrate_to)

    with pytest.raises(asyncpg.CheckViolationError):
        execute(disposable_database, ROW.format(ALICE, bad))
    assert scalar(disposable_database, COUNT) == 0


@pytest.mark.parametrize("good", ["a", "0", KEY, "nudge.install_v2", "k" * KEY_MAX_LENGTH])
def test_the_bounds_are_the_domains_and_both_edges_are_allowed(
    disposable_database: str, migrate_to: MigrateTo, good: str
) -> None:
    upgraded(disposable_database, migrate_to)

    execute(disposable_database, ROW.format(ALICE, good))
    assert scalar(disposable_database, COUNT) == 1


def test_deleting_a_user_deletes_their_notices(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, ROW.format(ALICE, KEY))
    execute(disposable_database, ROW.format(BOB, KEY))

    execute(disposable_database, DELETE_USER.format(ALICE))

    assert scalar(disposable_database, COUNT) == 1
    assert scalar(disposable_database, "SELECT user_id::text FROM user_dismissed_notices") == BOB

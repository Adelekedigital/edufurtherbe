"""What the `push_subscriptions` migration leaves behind (#386).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

from app.domain.push import (
    AUTH_MAX_LENGTH,
    ENDPOINT_MAX_LENGTH,
    ENDPOINT_PATTERN,
    P256DH_MAX_LENGTH,
)

pytestmark = pytest.mark.db

REVISION = "5b2e9d41c7a3"
PREVIOUS = "ebd36a4e11df"

ALICE = "00000000-0000-7000-8000-000000000001"
BOB = "00000000-0000-7000-8000-000000000002"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"

SEED_TEMPLATE = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('{alice}', gen_random_uuid(), 'a@example.test', 'mentee', 'UTC'),
  ('{bob}', gen_random_uuid(), 'b@example.test', 'mentor', 'UTC');
"""
SEED = SEED_TEMPLATE.format(alice=ALICE, bob=BOB)
ROW = (
    "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
    "VALUES ('{}', '{}', '{}', '{}')"
)
COUNT = "SELECT count(*) FROM push_subscriptions"
DELETE_USER = "DELETE FROM users WHERE id = '{}'"
MOVE_TO = "UPDATE push_subscriptions SET user_id = '{}'"


def row(
    user: str = ALICE, endpoint: str = ENDPOINT, p256dh: str = "p" * 87, auth: str = "a" * 22
) -> str:
    return ROW.format(user, endpoint, p256dh, auth)


def upgraded(url: str, migrate_to: MigrateTo) -> None:
    migrate_to(url, REVISION)
    execute(url, SEED)


@pytest.mark.parametrize("second_owner", [ALICE, BOB])
def test_a_subscription_is_stored_once_per_endpoint_whoever_owns_it(
    disposable_database: str, migrate_to: MigrateTo, second_owner: str
) -> None:
    upgraded(disposable_database, migrate_to)

    execute(disposable_database, row())
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM push_subscriptions WHERE id IS NOT NULL "
            "AND created_at IS NOT NULL AND updated_at IS NOT NULL",
        )
        == 1
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, row(user=second_owner))
    execute(disposable_database, row(endpoint=ENDPOINT + "-other"))
    assert scalar(disposable_database, COUNT) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"endpoint": "http://fcm.googleapis.com/fcm/send/abc"},
        {"endpoint": "https://" + "e" * (ENDPOINT_MAX_LENGTH - 7)},
        # Under the bound in characters, over it in bytes.
        {"endpoint": "https://" + "é" * (ENDPOINT_MAX_LENGTH // 2)},
        {"endpoint": "https://fcm.googleapis.com/fcm/send/a b"},
        {"endpoint": "https://"},
        {"p256dh": ""},
        {"p256dh": "p" * (P256DH_MAX_LENGTH + 1)},
        {"auth": ""},
        {"auth": "a" * (AUTH_MAX_LENGTH + 1)},
    ],
)
def test_the_database_refuses_a_subscription_out_of_bounds(
    disposable_database: str, migrate_to: MigrateTo, bad: dict[str, str]
) -> None:
    upgraded(disposable_database, migrate_to)

    with pytest.raises(asyncpg.CheckViolationError):
        execute(disposable_database, row(**bad))
    assert scalar(disposable_database, COUNT) == 0


def test_the_bounds_are_the_domains_and_both_edges_are_allowed(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)

    longest = "https://" + "e" * (ENDPOINT_MAX_LENGTH - 8)
    execute(
        disposable_database,
        row(endpoint=longest, p256dh="p" * P256DH_MAX_LENGTH, auth="a" * AUTH_MAX_LENGTH),
    )
    execute(disposable_database, row(endpoint=ENDPOINT, p256dh="p", auth="a"))
    assert scalar(disposable_database, COUNT) == 2


def test_deleting_a_user_deletes_their_subscriptions(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, row(user=ALICE))
    execute(disposable_database, row(user=BOB, endpoint=ENDPOINT + "-bob"))

    execute(disposable_database, DELETE_USER.format(ALICE))

    assert scalar(disposable_database, COUNT) == 1
    assert scalar(disposable_database, "SELECT user_id::text FROM push_subscriptions") == BOB


def test_an_update_moves_updated_at_and_a_users_rows_are_indexed(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, row())
    execute(
        disposable_database,
        "UPDATE push_subscriptions SET created_at = now() - interval '1 day', "
        "updated_at = now() - interval '1 day'",
    )

    execute(disposable_database, MOVE_TO.format(BOB))

    assert scalar(disposable_database, "SELECT updated_at > created_at FROM push_subscriptions")
    assert scalar(
        disposable_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_push_subscriptions_user_id'",
    ).endswith("(user_id)")


def test_the_downgrade_drops_the_table(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, row())

    command.downgrade(make_alembic_config(disposable_database), PREVIOUS)

    assert scalar(disposable_database, "SELECT to_regclass('push_subscriptions')") is None


def test_the_endpoint_constraint_spells_the_domains_pattern(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    """The migration spells the pattern out; this pins it to the constant the
    model (and the request model) reads."""
    upgraded(disposable_database, migrate_to)

    body = scalar(
        disposable_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_push_subscriptions_endpoint_shape'",
    )

    assert f"'{ENDPOINT_PATTERN}'" in body
    assert f"octet_length(endpoint) <= {ENDPOINT_MAX_LENGTH}" in body

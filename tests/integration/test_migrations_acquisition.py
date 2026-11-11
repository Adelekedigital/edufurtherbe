"""What `f6b1d3a8c925` changes (PRD 30 PR 2): the acquisition, internal and
entry-point columns, `analytics_consents`, and the two backfills on rows that
predate them.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

pytestmark = pytest.mark.db

REVISION = "f6b1d3a8c925"
PREVIOUS = "e2a9c4f7b153"

#: Five accounts before the upgrade, each stamped with one Bubble Modified Date.
SEED = """
INSERT INTO users (email, legacy_bubble_id, updated_at) VALUES
  ('member@example.test', NULL, '2025-11-06 11:52:41.383+00'),
  ('migrated@example.test', 'bubble-1', '2025-11-06 11:52:41.383+00'),
  ('admin@example.test', 'bubble-2', '2025-11-06 11:52:41.383+00'),
  ('revoked@example.test', NULL, '2025-11-06 11:52:41.383+00'),
  ('seed@demo.edufurther.test', NULL, '2025-11-06 11:52:41.383+00');
INSERT INTO admin_users (user_id, admin_role)
  SELECT id, 'super_admin' FROM users WHERE email = 'admin@example.test';
INSERT INTO admin_users (user_id, admin_role, revoked_at)
  SELECT id, 'super_admin', now() FROM users WHERE email = 'revoked@example.test';
"""

ROWS = (
    "SELECT string_agg(split_part(email, '@', 1) || ':' || is_internal || ':' || signup_source, "
    "',' ORDER BY email) FROM users"
)


def seeded(url: str, migrate_to: MigrateTo, config: ConfigFactory) -> None:
    migrate_to(url, PREVIOUS)
    execute(url, SEED)
    command.upgrade(config(url), REVISION)


def test_the_backfills_mark_internal_and_migrated_accounts(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    seeded(disposable_database, migrate_to, make_alembic_config)

    assert scalar(disposable_database, ROWS) == (
        "admin:true:migrated,member:false:direct,migrated:false:migrated,"
        "revoked:false:direct,seed:true:direct"
    )


def test_every_existing_account_has_its_acquisition_settled(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    seeded(disposable_database, migrate_to, make_alembic_config)

    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM users WHERE acquired_at IS DISTINCT FROM created_at",
        )
        == 0
    )
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM pg_proc WHERE proname IN ('internal_account', "
            "'mark_internal_user', 'mark_internal_grantee') "
            "AND proconfig @> ARRAY['search_path=public']",
        )
        == 3
    )


def test_the_backfills_keep_bubbles_modified_date(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    seeded(disposable_database, migrate_to, make_alembic_config)

    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM users WHERE updated_at <> '2025-11-06 11:52:41.383+00'",
        )
        == 0
    )
    assert (
        scalar(
            disposable_database,
            "SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_set_updated_at' "
            "AND tgrelid = 'users'::regclass",
        )
        == b"O"
    )


def test_a_grant_after_the_upgrade_is_projected(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    seeded(disposable_database, migrate_to, make_alembic_config)

    execute(
        disposable_database,
        "INSERT INTO admin_users (user_id, admin_role) "
        "SELECT id, 'limited_access' FROM users WHERE email = 'member@example.test'",
    )

    assert scalar(
        disposable_database, "SELECT is_internal FROM users WHERE email = 'member@example.test'"
    )


def test_a_value_outside_each_vocabulary_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    seeded(disposable_database, migrate_to, make_alembic_config)

    for statement in (
        "UPDATE users SET signup_source = 'facebook'",
        "UPDATE users SET signup_channel = 'tiktok'",
        "UPDATE users SET auth_method = 'password'",
    ):
        with pytest.raises(Exception, match="check constraint"):
            execute(disposable_database, statement)


def test_downgrade_drops_it_all_and_upgrade_restores_it(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    config = make_alembic_config(disposable_database)
    seeded(disposable_database, migrate_to, make_alembic_config)
    execute(
        disposable_database,
        "INSERT INTO analytics_consents (user_id, granted, version) "
        "SELECT id, false, 'v1' FROM users WHERE email = 'member@example.test'",
    )

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, "SELECT to_regclass('analytics_consents')") is None
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM information_schema.columns WHERE "
            "(table_name = 'users' AND column_name IN ('is_internal', 'signup_source', "
            "'signup_channel', 'acquired_at', 'auth_method')) "
            "OR (table_name = 'sessions' AND column_name = 'entry_point')",
        )
        == 0
    )
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM pg_proc WHERE proname IN "
            "('internal_account', 'mark_internal_user', 'mark_internal_grantee')",
        )
        == 0
    )
    # A grant still writes with the triggers gone.
    execute(
        disposable_database,
        "INSERT INTO admin_users (user_id, admin_role) "
        "SELECT id, 'limited_access' FROM users WHERE email = 'member@example.test'",
    )

    command.upgrade(config, REVISION)

    assert "member:true:direct" in scalar(disposable_database, ROWS)

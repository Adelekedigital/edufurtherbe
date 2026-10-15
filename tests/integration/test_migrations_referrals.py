"""What the referral migrations leave behind, beyond "the chain runs".

Split from `test_migrations.py` by area when it reached the file-size limit
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db


def test_the_revocation_downgrade_refuses_to_reactivate_a_revoked_link(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Dropping `revoked_at` would switch every revoked link back on, with
    nobody choosing that. So `d7b2f4a8c615`'s downgrade stops while one is
    revoked, and proceeds once it is reactivated."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "head")
    execute(
        disposable_database,
        """
        WITH u AS (
            INSERT INTO users (email, auth_id, first_name, primary_role, timezone)
            VALUES ('revoked@example.test', gen_random_uuid(), 'Probe', 'mentee', 'UTC')
            RETURNING id
        )
        INSERT INTO invite_links (user_id, code, revoked_at) SELECT id, 'switched-off', now() FROM u
        """,
    )

    with pytest.raises(RuntimeError, match="revoked"):
        command.downgrade(config, "c5a1e7f3d920")

    execute(disposable_database, "UPDATE invite_links SET revoked_at = NULL")
    command.downgrade(config, "c5a1e7f3d920")

    assert scalar(disposable_database, "SELECT code FROM invite_links") == "switched-off"

"""The backfill that marks loaded Bubble mentors as submitted (#660).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import json

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

pytestmark = pytest.mark.db

REVISION = "e8b2c5f1a7d3"
PREVIOUS = "d4e7a1c95b26"

_MENTOR = (
    "WITH u AS (INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentor', 'UTC') RETURNING id) "
    "INSERT INTO mentor_profiles (user_id, legacy_bubble_id, submitted_at, created_at, updated_at) "
    "SELECT id, {legacy}, {submitted}, timestamptz '2025-03-04 05:06:07+00', "
    "timestamptz '2025-06-07 08:09:10+00' FROM u"
)
_STAMPS = (
    "SELECT json_object_agg(u.email, m.submitted_at = m.created_at) "
    "FROM mentor_profiles m JOIN users u ON u.id = m.user_id"
)
_UNTOUCHED = (
    "SELECT count(*) FROM mentor_profiles WHERE updated_at = timestamptz '2025-06-07 08:09:10+00'"
)


def test_a_loaded_mentor_counts_as_submitted_and_a_draft_does_not(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, PREVIOUS)
    rows = {
        "loaded@example.test": ("'1700000000000x1'", "NULL"),
        "loaded-submitted@example.test": ("'1700000000000x2'", "timestamptz '2026-01-01+00'"),
        "draft@example.test": ("NULL", "NULL"),
    }
    for email, (legacy, submitted) in rows.items():
        execute(
            disposable_database, _MENTOR.format(email=email, legacy=legacy, submitted=submitted)
        )

    command.upgrade(make_alembic_config(disposable_database), REVISION)

    assert json.loads(scalar(disposable_database, _STAMPS)) == {
        "loaded@example.test": True,
        "loaded-submitted@example.test": False,
        "draft@example.test": None,
    }
    assert scalar(disposable_database, _UNTOUCHED) == 3

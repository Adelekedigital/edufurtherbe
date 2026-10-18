"""The migration that records a mentor application's submission and its approval
being seen, and renames the help areas to sentence case (Mentor Onboarding B1).

Its own file because `test_migrations.py` is at the size limit (rule 11).
"""

from __future__ import annotations

import json

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "b3e9d1f6a2c8"
PREVIOUS = "a8d2e5c7f391"

SENTENCE_CASE = {
    "test-preparation": "Test preparation",
    "document-preparation": "Document preparation",
    "school-selection": "School selection",
    "program-selection": "Program selection",
    "scholarships-financial-aid": "Scholarships and financial aid",
    "interview-preparation": "Interview preparation",
}
TITLE_CASE = {
    "test-preparation": "Test Preparation",
    "document-preparation": "Document Preparation",
    "school-selection": "School Selection",
    "program-selection": "Program Selection",
    "scholarships-financial-aid": "Scholarships & Financial Aid",
    "interview-preparation": "Interview Preparation",
}

_MENTOR = (
    "WITH u AS (INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentor', 'UTC') RETURNING id) "
    "INSERT INTO mentor_profiles (user_id, approval_status, created_at, updated_at) "
    "SELECT id, '{status}', timestamptz '2025-03-04 05:06:07+00', "
    "timestamptz '2025-06-07 08:09:10+00' FROM u"
)


_PROFILES_UPDATED = (
    "SELECT count(*) FROM mentor_profiles WHERE updated_at = timestamptz '2025-06-07 08:09:10+00'"
)
_OFFERINGS_UPDATED = "SELECT json_object_agg(slug, updated_at) FROM service_offerings"


def _names(url: str) -> dict[str, str]:
    rows = scalar(
        url,
        "SELECT json_object_agg(slug, display_name) FROM service_offerings",
    )
    return dict(json.loads(rows))


def _stamps(url: str) -> dict[str, list[bool | None]]:
    """Each mentor's `[submitted_at = created_at, approved_seen_at = created_at]`,
    by email; a null column reads `None`."""
    rows = scalar(
        url,
        "SELECT json_object_agg(u.email, json_build_array("
        "m.submitted_at = m.created_at, m.approved_seen_at = m.created_at)) "
        "FROM mentor_profiles m JOIN users u ON u.id = m.user_id",
    )
    return dict(json.loads(rows))


def test_existing_mentors_count_as_submitted_and_only_approved_ones_as_seen(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Every existing profile was an application under the old rule (creating it
    alerted admins), so it is submitted. Only an approved one has been seen: a
    pending one marked seen would never show the approved screen."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    execute(disposable_database, _MENTOR.format(email="ok@example.test", status="approved"))
    execute(disposable_database, _MENTOR.format(email="wait@example.test", status="pending"))
    execute(disposable_database, _MENTOR.format(email="no@example.test", status="declined"))

    command.upgrade(config, REVISION)

    assert _stamps(disposable_database) == {
        "ok@example.test": [True, True],
        "wait@example.test": [True, None],
        "no@example.test": [True, None],
    }


def test_the_backfill_and_the_renames_leave_updated_at_alone(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """`updated_at` carries Bubble's Modified Date and the featured pick's
    freshness; a backfill that moved it would make every mentor look edited today."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    execute(disposable_database, _MENTOR.format(email="ok@example.test", status="approved"))
    offerings_before = scalar(disposable_database, _OFFERINGS_UPDATED)

    command.upgrade(config, REVISION)

    assert scalar(disposable_database, _PROFILES_UPDATED) == 1
    assert scalar(disposable_database, _OFFERINGS_UPDATED) == offerings_before
    command.downgrade(config, PREVIOUS)
    assert scalar(disposable_database, _OFFERINGS_UPDATED) == offerings_before


def test_a_profile_created_after_the_migration_is_neither_submitted_nor_seen(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    command.upgrade(make_alembic_config(disposable_database), REVISION)
    execute(disposable_database, _MENTOR.format(email="new@example.test", status="pending"))

    assert _stamps(disposable_database) == {"new@example.test": [None, None]}


def test_the_help_areas_are_renamed_to_sentence_case_and_back(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """All six by slug: a typo in one slug would rename nothing and pass a count."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    execute(disposable_database, _MENTOR.format(email="ok@example.test", status="approved"))
    assert _names(disposable_database) == TITLE_CASE

    command.upgrade(config, REVISION)
    assert _names(disposable_database) == SENTENCE_CASE
    # The re-upgrade backfills again, which is what makes the downgrade safe.
    assert _stamps(disposable_database) == {"ok@example.test": [True, True]}

    command.downgrade(config, PREVIOUS)
    assert _names(disposable_database) == TITLE_CASE
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM information_schema.columns WHERE table_name = "
            "'mentor_profiles' AND column_name IN ('submitted_at', 'approved_seen_at')",
        )
        == 0
    )
    command.upgrade(config, REVISION)
    assert _names(disposable_database) == SENTENCE_CASE
    # The re-upgrade backfills again, which is what makes the downgrade safe.
    assert _stamps(disposable_database) == {"ok@example.test": [True, True]}

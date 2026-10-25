"""What `b8d2e6f4a3c7` does to stored stages (decision 288, step 3a).

Every case is written at the previous revision, with SQL, because the app's
own writes can no longer produce a retired stage; then the chain upgrades over
them. Split from `test_migrations.py` by area (non-negotiable 11).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.sessions import RETIRED_STAGES

pytestmark = pytest.mark.db

REVISION = "b8d2e6f4a3c7"
PREVIOUS = "a1f4c8e2d6b9"

MENTOR = "00000000-0000-7000-8000-000000000001"

#: Each case: a type's stored set (in order), its legacy column and label, and
#: what the migration must leave: the set, then the legacy column.
CASES: dict[str, tuple[list[str], str | None, str | None, list[str], str | None]] = {
    "merged-and-other": (
        ["revisions", "other", "early_exploration", "drafting_stage"],
        "revisions",
        None,
        ["preparing_documents", "exploring"],
        "preparing_documents",
    ),
    "other-first-with-label": (
        ["other", "interviewing"],
        "other",
        "Gap year",
        ["interview_prep"],
        "interview_prep",
    ),
    "only-other": (["other"], "other", "Gap year", [], None),
    "already-current": (
        ["applying", "researching"],
        "applying",
        None,
        ["applying", "researching"],
        "applying",
    ),
    "current-and-retired-collide": (
        ["preparing_documents", "drafting_stage", "post_submission"],
        "preparing_documents",
        None,
        ["preparing_documents", "awaiting_decision"],
        "preparing_documents",
    ),
    "legacy-only": ([], "post_submission", None, [], "awaiting_decision"),
    "legacy-only-other": ([], "other", "Gap year", [], None),
    "no-stage": ([], None, None, [], None),
    # The database's CHECK holds one direction only, so a label can sit beside a
    # current stage; it goes even though the stage itself does not change.
    "stray-label": (["applying"], "applying", "Stray", ["applying"], "applying"),
}


def type_id(index: int) -> str:
    return f"00000000-0000-7000-8000-0000000001{index:02d}"


def seed(url: str) -> None:
    execute(
        url,
        "INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES "  # noqa: S608
        f"('{MENTOR}', gen_random_uuid(), 'm@example.test', 'mentor', 'UTC'); "
        f"INSERT INTO mentor_profiles (user_id) VALUES ('{MENTOR}')",
    )
    for index, (name, (stages, legacy, label, _, _)) in enumerate(CASES.items()):
        tid = type_id(index)
        column = "NULL" if legacy is None else f"'{legacy}'"
        text = "NULL" if label is None else f"'{label}'"
        execute(
            url,
            "INSERT INTO session_types (id, mentor_user_id, name, application_stage, "  # noqa: S608
            f"custom_stage_label) VALUES ('{tid}', '{MENTOR}', '{name}', {column}, {text})",
        )
        for position, stage in enumerate(stages):
            execute(
                url,
                "INSERT INTO session_type_stages (session_type_id, stage, position) "  # noqa: S608
                f"VALUES ('{tid}', '{stage}', {position})",
            )


def stored(url: str, index: int) -> tuple[list[Any], Any, Any]:
    tid = type_id(index)
    stages = scalar(
        url,
        "SELECT coalesce(array_agg(stage ORDER BY position), '{}') "  # noqa: S608
        f"FROM session_type_stages WHERE session_type_id = '{tid}'",
    )
    positions = scalar(
        url,
        "SELECT coalesce(array_agg(position ORDER BY position), '{}') "  # noqa: S608
        f"FROM session_type_stages WHERE session_type_id = '{tid}'",
    )
    assert list(positions) == list(range(len(stages))), "positions are renumbered from zero"
    legacy = scalar(url, f"SELECT application_stage FROM session_types WHERE id = '{tid}'")  # noqa: S608
    label = scalar(url, f"SELECT custom_stage_label FROM session_types WHERE id = '{tid}'")  # noqa: S608
    return list(stages), legacy, label


def test_every_stored_stage_becomes_one_of_the_six(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    seed(disposable_database)

    command.upgrade(config, REVISION)

    for index, (name, (_, _, _, want_stages, want_legacy)) in enumerate(CASES.items()):
        assert stored(disposable_database, index) == (want_stages, want_legacy, None), name


def test_running_it_on_current_data_changes_nothing(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Idempotent in effect: a type already on the six keeps its rows and its
    `updated_at`, so an offering nobody touched shows no edit."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    seed(disposable_database)
    current = list(CASES).index("already-current")
    before = scalar(
        disposable_database,
        f"SELECT updated_at FROM session_types WHERE id = '{type_id(current)}'",  # noqa: S608
    )

    command.upgrade(config, REVISION)

    after = scalar(
        disposable_database,
        f"SELECT updated_at FROM session_types WHERE id = '{type_id(current)}'",  # noqa: S608
    )
    assert before == after


def test_the_migration_s_copy_of_the_remap_is_the_code_s() -> None:
    """No migration imports from `app` (decision 105), so its `MAP` is a copy;
    this is what stops the two disagreeing (non-negotiable 8)."""
    path = next(Path("migrations/versions").glob(f"*_{REVISION}.py"))
    spec = importlib.util.spec_from_file_location("remap_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    code = {old.value: None if new is None else new.value for old, new in RETIRED_STAGES.items()}
    assert code == module.MAP

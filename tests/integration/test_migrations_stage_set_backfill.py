"""What `d9a3f6b1c2e8` does: a type staged only in the column gets its set row
(decision 293), and nothing else changes.

Written at the previous revision with SQL, because the app's own writes always
fill the set. Split from `test_migrations.py` by area (non-negotiable 11).
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "d9a3f6b1c2e8"
PREVIOUS = "c4e7a9b2d5f8"

MENTOR = "00000000-0000-7000-8000-000000000001"

#: Each case: the stored set (in order) and the legacy column, then the set the
#: migration must leave.
CASES: dict[str, tuple[list[str], str | None, list[str]]] = {
    "column-only": ([], "applying", ["applying"]),
    "set-and-column": (["researching", "applying"], "researching", ["researching", "applying"]),
    # The column disagrees with a set the mentor wrote: the set wins, as reads did.
    "set-disagrees": (["exploring"], "applying", ["exploring"]),
    "no-stage": ([], None, []),
}


def type_id(index: int) -> str:
    return f"00000000-0000-7000-8000-0000000002{index:02d}"


def seed(url: str) -> None:
    execute(
        url,
        "INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES "  # noqa: S608
        f"('{MENTOR}', gen_random_uuid(), 'm@example.test', 'mentor', 'UTC'); "
        f"INSERT INTO mentor_profiles (user_id) VALUES ('{MENTOR}')",
    )
    for index, (name, (stages, legacy, _)) in enumerate(CASES.items()):
        tid = type_id(index)
        column = "NULL" if legacy is None else f"'{legacy}'"
        execute(
            url,
            "INSERT INTO session_types (id, mentor_user_id, name, application_stage) "  # noqa: S608
            f"VALUES ('{tid}', '{MENTOR}', '{name}', {column})",
        )
        for position, stage in enumerate(stages):
            execute(
                url,
                "INSERT INTO session_type_stages (session_type_id, stage, position) "  # noqa: S608
                f"VALUES ('{tid}', '{stage}', {position})",
            )


def stored(url: str, index: int) -> tuple[list[Any], list[Any]]:
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
    return list(stages), list(positions)


def test_a_type_staged_only_in_the_column_gets_its_set_row(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    seed(disposable_database)

    command.upgrade(config, REVISION)

    for index, (name, (_, legacy, want)) in enumerate(CASES.items()):
        stages, positions = stored(disposable_database, index)
        assert (stages, positions) == (want, list(range(len(want)))), name
        column = scalar(
            disposable_database,
            f"SELECT application_stage FROM session_types WHERE id = '{type_id(index)}'",  # noqa: S608
        )
        assert column == legacy, f"{name}: the column is left as it was"


def test_running_it_twice_adds_nothing(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Idempotent: once a type has its row, a rerun finds nothing to copy."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    seed(disposable_database)
    command.upgrade(config, REVISION)
    command.downgrade(config, PREVIOUS)

    command.upgrade(config, REVISION)

    only = list(CASES).index("column-only")
    assert stored(disposable_database, only) == (["applying"], [0])

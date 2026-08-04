"""Structural checks on the migration chain that need no database.

Deliberately in the fast tier. This is the cheapest check in the project and it
catches a failure that otherwise shows up at deploy time, so it must run on every
machine — including one with no Docker — and inside the pre-commit hook.
"""

from collections.abc import Callable

from alembic.config import Config
from alembic.script import ScriptDirectory

ConfigFactory = Callable[[str], Config]

# Any syntactically valid DSN. Nothing connects: ScriptDirectory reads
# script_location off the config and never opens a connection.
UNUSED_DSN = "postgresql://unused/unused"


def test_there_is_exactly_one_head(make_alembic_config: ConfigFactory) -> None:
    """Two heads is a merge artifact, and it breaks deploys with an error that
    names neither migration involved."""
    script = ScriptDirectory.from_config(make_alembic_config(UNUSED_DSN))

    assert len(script.get_heads()) == 1

"""Fixtures shared across every tier."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Explicit settings, so a test never depends on the developer's environment.

    ``_env_file=None`` is load-bearing. Without it the fixture still reads
    whatever ``.env`` happens to sit in the working directory, and any field not
    pinned below silently takes that file's value — init arguments outrank a
    dotenv value, so the fields named here hid the leak rather than preventing it.
    """
    return Settings(_env_file=None, environment="ci", debug=False)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client

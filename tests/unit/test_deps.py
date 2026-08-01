"""Dependency wiring.

``deps.py`` is a composition point exempt from the layer check, so nothing else
verifies it. Without this the module sat at 0% coverage — the wiring could stop
resolving and no test would notice.
"""

from typing import get_args

from app.api.deps import SettingsDep
from app.core.config import Settings


def test_settings_dependency_resolves_to_settings() -> None:
    annotated_type, dependency = get_args(SettingsDep)

    assert annotated_type is Settings
    assert dependency.dependency is not None
    assert dependency.dependency() == Settings()

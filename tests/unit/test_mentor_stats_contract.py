"""A dashboard window's bounds are never null (decision 246).

Every figure is optional (`test_added_fields_stay_optional`); the bounds are not,
because a window without them cannot be placed and no default instant is true.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas.mentor_stats import MentorStatsWindowRead
from app.core.config import Settings
from app.main import create_app


@pytest.mark.parametrize("bound", ["since", "until"])
def test_a_bound_is_required_and_never_null_in_the_spec(bound: str) -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]  # type: ignore[call-arg]
    window = spec["MentorStatsWindowRead"]

    assert bound in window.get("required", [])
    assert window["properties"][bound]["type"] == "string"
    assert "anyOf" not in window["properties"][bound]


def test_a_window_cannot_be_built_without_its_bounds() -> None:
    with pytest.raises(ValidationError, match="since"):
        MentorStatsWindowRead()  # type: ignore[call-arg]

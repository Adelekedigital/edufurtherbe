"""A dashboard window's bounds are never null (decisions 246, 249).

Every figure is optional (`test_added_fields_stay_optional`); the bounds are not,
because a window without them cannot be placed and no default instant is true.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas.mentee_dashboard import MenteeDashboardRead, MenteeDashboardWindowRead
from app.api.schemas.mentor_stats import MentorStatsRead, MentorStatsWindowRead
from app.core.config import Settings
from app.main import create_app

#: Each dashboard, with its window model.
DASHBOARDS = [
    (MentorStatsRead, MentorStatsWindowRead),
    (MenteeDashboardRead, MenteeDashboardWindowRead),
]


@pytest.mark.parametrize("window_model", [window for _, window in DASHBOARDS])
@pytest.mark.parametrize("bound", ["since", "until"])
def test_a_bound_is_required_and_never_null_in_the_spec(window_model: type, bound: str) -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]  # type: ignore[call-arg]
    window = spec[window_model.__name__]

    assert bound in window.get("required", [])
    assert window["properties"][bound]["type"] == "string"
    assert "anyOf" not in window["properties"][bound]


@pytest.mark.parametrize("window_model", [window for _, window in DASHBOARDS])
def test_a_window_cannot_be_built_without_its_bounds(window_model: type) -> None:
    with pytest.raises(ValidationError, match="since"):
        window_model()


@pytest.mark.parametrize("dashboard", [dashboard for dashboard, _ in DASHBOARDS])
@pytest.mark.parametrize("block", ["current", "previous"])
def test_both_blocks_are_required(dashboard: type, block: str) -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]  # type: ignore[call-arg]

    assert block in spec[dashboard.__name__].get("required", [])

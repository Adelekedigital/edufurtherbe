"""Which paths the per-viewer cache headers cover."""

from __future__ import annotations

import pytest

from app.api.per_viewer import _is_per_viewer


@pytest.mark.parametrize(
    ("path", "covered"),
    [
        ("/api/v1/mentors", True),
        ("/api/v1/mentors/ada", True),
        ("/api/v1/mentors/ada/reviews", True),
        # A sibling sharing the prefix is a different resource.
        ("/api/v1/mentors-archive", False),
        ("/api/v1/featured-mentor", False),
        ("/api/v1/me", False),
    ],
)
def test_the_root_and_its_subpaths_only(path: str, covered: bool) -> None:
    assert _is_per_viewer(path) is covered

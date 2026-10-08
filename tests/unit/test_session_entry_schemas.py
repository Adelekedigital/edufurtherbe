"""`/join` and `/door` publish their responses as declared schemas.

Both once returned an untyped dict, which published the 200 as an arbitrary
object: a client generated from the spec got no `meeting_url` at all. Codex
caught it on `/door` (#380); `/join` had the same defect. A response model is one
line to drop in a refactor and nothing else would notice, so the spec is pinned.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_app


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return dict(create_app(Settings(_env_file=None)).openapi())


@pytest.mark.parametrize(
    ("path", "schema", "required"),
    [
        ("/api/v1/sessions/{session_id}/join", "JoinRead", {"joined", "meeting_url"}),
        ("/api/v1/sessions/{session_id}/door", "DoorRead", {"meeting_url"}),
    ],
)
def test_the_entry_endpoints_publish_their_response(
    document: dict[str, Any], path: str, schema: str, required: set[str]
) -> None:
    ok = document["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]

    assert ok["schema"] == {"$ref": f"#/components/schemas/{schema}"}
    published = document["components"]["schemas"][schema]
    assert set(published["required"]) == required
    assert set(published["properties"]) == required

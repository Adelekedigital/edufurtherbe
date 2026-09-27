"""The published API spec is exactly what the running API serves.

The frontend generates its client from the file the `publish-openapi` workflow
uploads. If the export built its own document — or ran with different settings
from the server — the client would describe an API that does not exist.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.export_openapi import main

from app.main import app


def test_the_export_is_the_document_the_api_serves(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"

    assert main(["--out", str(out)]) == 0

    assert json.loads(out.read_text(encoding="utf-8")) == app.openapi()


def test_the_export_carries_the_public_discovery_routes(tmp_path: Path) -> None:
    """The positive case: a document with no paths would compare equal to an
    app that registered no routers, and the client would generate nothing."""
    out = tmp_path / "openapi.json"
    main(["--out", str(out)])

    paths = json.loads(out.read_text(encoding="utf-8"))["paths"]

    assert "/api/v1/mentors" in paths
    assert "/api/v1/catalog/{catalogue}" in paths

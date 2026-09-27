"""Write the API's OpenAPI document to a file — exactly what `/openapi.json` serves.

The `publish-openapi` workflow runs this on every push to `main` and uploads the
result to the rolling `openapi-latest` release, which the frontend's CI downloads
to generate its client. Run it locally for the same file:

    uv run python scripts/export_openapi.py --out openapi.json

**`app.openapi()`, not a document built here.** It is the object FastAPI serves,
so the published spec cannot describe a route the API does not have. Building
the app reads settings, but it needs no secrets and opens no database
connection, so this runs in CI with no environment at all.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.infra.etl.cli import EXIT_OK
from app.main import app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="Where to write the JSON.")
    args = parser.parse_args(argv)
    args.out.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

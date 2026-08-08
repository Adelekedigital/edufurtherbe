"""Pull the legacy Bubble snapshot to disk, or inspect it without writing.

    # what the API would give us, writing nothing
    uv run python scripts/extract_bubble.py --thing user --dry-run

    # the same inspection against an export you already have
    uv run python scripts/extract_bubble.py --thing allusers --from-export script-data-dev --dry-run

    # write it
    uv run python scripts/extract_bubble.py --thing user

Writes ``data/bubble/<thing>.json``. ``data/`` is gitignored — these files hold
1,200 members' names, emails and profile text, and `gitleaks` finds credentials
rather than people, so nothing in the gate would object to them being committed.

Credentials are dropped by the reader before a record reaches this script, so
there is no redaction step here to forget. The control sits in the shared
canonicalisation both sources go through, not at each call site — which is also
why ``--dry-run`` can report on credential fields honestly: if it says none were
seen, that is the reader having removed them, not this script having missed them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.domain.bubble import EXPORT_TIMEZONE, REDACTED_FIELDS
from app.infra.clients.bubble import BubbleApiSource, BubbleSourceError, JsonExportSource

DEFAULT_OUTPUT = Path("data/bubble")

# Established by measurement, not assumption: the same user reads
# `Dec 7, 2023 1:36 pm` in the export and `2023-12-07T18:36:46.179Z` from the
# API, and 13:36 EST is 18:36 UTC.


def summarise(thing: str, records: list[dict[str, Any]]) -> None:
    """Report shape without printing a single field value.

    A dry run over member data must not put names or emails on a terminal or
    into CI logs, so this prints counts and key names only. The exception would
    be a credential value, and those cannot appear here because the reader
    removed them before this script saw the record.
    """
    keys: dict[str, int] = {}
    for record in records:
        for key, value in record.items():
            if value not in (None, [], {}):
                keys[key] = keys.get(key, 0) + 1

    print(f"\n{thing}: {len(records)} records, {len(keys)} distinct populated fields")
    for key, count in sorted(keys.items(), key=lambda item: (-item[1], item[0])):
        print(f"   {count:>5}  {key}")

    leaked = sorted({field for record in records for field in REDACTED_FIELDS if field in record})
    print(f"   credential fields present: {leaked or 'none'}")

    missing_email = sum(1 for record in records if not record.get("email"))
    with_identity = sum(1 for record in records if record.get("provider_identities"))
    print(f"   without an email: {missing_email}   with a linked provider: {with_identity}")


def open_source(args: argparse.Namespace) -> Iterator[Any]:
    """Yield the configured source, closing the HTTP client if one was opened."""
    if args.from_export:
        yield JsonExportSource(args.from_export, timezone=EXPORT_TIMEZONE)
        return

    settings = get_settings()
    if not settings.bubble_api_url or not settings.bubble_api_token:
        # Raised here rather than making the settings required: importing the
        # application must not depend on credentials only an extract needs.
        raise BubbleSourceError(
            "Set BUBBLE_API_URL and BUBBLE_API_TOKEN, or "
            "pass --from-export. The URL carries the environment; /version-test/ "
            "is the dev app."
        )

    # Generous timeout: a one-off extract against a rate-limited API, where a
    # retry costs more than a slow response.
    with httpx.Client(timeout=30.0) as client:
        yield BubbleApiSource(
            settings.bubble_api_url, settings.bubble_api_token.get_secret_value(), client
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--thing", action="append", required=True, help="Bubble Thing; repeat for several"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--from-export",
        type=Path,
        help="read a Data-tab export directory instead of the API",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and report shape; write nothing",
    )
    args = parser.parse_args()

    # Bubble field names contain emoji — `👥Role`, `Admin 🎩`. A Windows console
    # defaults to cp1252 and raises UnicodeEncodeError on the first one, which
    # kills a diagnostic run halfway through its own output. `backslashreplace`
    # degrades an unrenderable character to an escape rather than losing it, so
    # the key is still identifiable on a terminal that cannot draw it.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    try:
        for source in open_source(args):
            if not args.dry_run:
                args.output.mkdir(parents=True, exist_ok=True)

            for thing in args.thing:
                records = list(source.read(thing))

                if args.dry_run:
                    summarise(thing, records)
                    continue

                destination = args.output / f"{thing}.json"
                destination.write_text(
                    json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"{thing}: {len(records)} records -> {destination}")
    except BubbleSourceError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.dry_run:
        print("\ndry run — nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

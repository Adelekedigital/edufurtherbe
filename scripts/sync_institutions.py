"""Manually recover or dry-run the QStash-owned institution mirror job."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, EXIT_UNRESOLVED, configure_streams
from app.infra.jobs.runner import RuntimeJobs


async def run(args: argparse.Namespace) -> int:
    try:
        result = await RuntimeJobs(
            get_settings(), institution_file=args.from_file, reporter=print
        ).run("sync-institutions", dry_run=args.dry_run)
    except ValidationError:
        print("\nno usable rows â€” refusing to sync")
        return EXIT_REFUSED
    if result.counts.get("unmatched", 0) or result.counts.get("ambiguous", 0):
        return EXIT_UNRESOLVED
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-file", type=Path, help="Read a saved catalogue snapshot.")
    parser.add_argument("--dry-run", action="store_true")
    configure_streams()
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

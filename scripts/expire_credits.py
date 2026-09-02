"""Manually recover or dry-run the QStash-owned credit expiry job."""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.infra.etl.cli import EXIT_OK, configure_streams
from app.infra.jobs.runner import RuntimeJobs


async def run(args: argparse.Namespace) -> int:
    result = await RuntimeJobs(get_settings()).run("expire-credits", dry_run=args.dry_run)
    print(f"{result.status}: {result.counts}")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    configure_streams()
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

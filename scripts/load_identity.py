"""Transform a legacy snapshot into ``users``, then reconcile.

    # transform and report, touching no database
    uv run python scripts/load_identity.py --from-export script-data-dev --thing allusers --dry-run

    # load, then reconcile
    uv run python scripts/load_identity.py --from-export script-data-dev --thing allusers

A dry run is not a rehearsal of the write — it is the pre-load check the runbook
asks for. Duplicate emails and unmappable values are found *before* anything is
touched, rather than by a constraint halfway through a load with rows already
written and an operator holding a constraint name.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domain.transform import TransformReport, transform_users
from app.infra.clients.bubble import JsonExportSource
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.loader import UserLoader
from app.infra.etl.reconcile import reconcile_users

# Duplicated rather than imported from `scripts/extract_bubble.py`: `scripts/`
# is not a package, so a cross-script import resolves only when the repository
# root happens to be on sys.path — true under pytest, false under
# `uv run python scripts/...`. One constant is cheaper than making scripts
# importable, and a test asserts the two stay equal.
EXPORT_TIMEZONE = ZoneInfo("America/New_York")


def describe(report: TransformReport) -> None:
    """Counts and identifiers only — never a field value.

    The same rule as the extract's dry run: a diagnostic over member data must
    not put names or emails into a terminal or a CI log. A duplicate email is the
    one exception, and it is printed because the operator cannot resolve it
    without knowing which address collided.
    """
    print(f"transformed {len(report.rows)} rows, {len(report.errors)} refused")
    for error in report.errors:
        print(f"   refused  {error}")
    for email, ids in report.duplicate_emails.items():
        print(f"   DUPLICATE {email}: {', '.join(ids)}")


async def run(args: argparse.Namespace) -> int:
    source = JsonExportSource(args.from_export, timezone=EXPORT_TIMEZONE)
    records = list(source.read(args.thing))
    report = transform_users(records, export_timezone=EXPORT_TIMEZONE)
    describe(report)

    if not report.ok:
        print("\nrefusing to load — resolve the above first", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            written = await UserLoader(connection).load(report.rows)
            # Inside the same transaction as the load, deliberately: reconciling
            # after a commit would report on a database somebody else may have
            # touched, and reconciling a load you have not yet committed is the
            # only way to refuse to commit it.
            result = await reconcile_users(connection, report.rows)
            print(f"\nloaded {written}\n{result.report()}")
            if not result.ok:
                raise RuntimeError("reconciliation failed; rolling back")
    finally:
        await engine.dispose()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-export", type=Path, required=True)
    parser.add_argument("--thing", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

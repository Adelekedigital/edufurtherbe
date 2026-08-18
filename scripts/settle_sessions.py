"""Decide the outcome of every session whose join window has shut.

    # settle, and report how many
    uv run python scripts/settle_sessions.py

    # report what would be settled, touching nothing
    uv run python scripts/settle_sessions.py --dry-run

**A script rather than a scheduled job inside the application**, because settled
decision #13 uses no platform-native queue or cron: the escape from FastAPI Cloud
stays real only if nothing depends on its scheduler. An external timer runs this,
exactly as the weekly institution mirror is run.

**Safe to run twice, and it will be.** An external scheduler is the kind that
fires twice — the participant update touches only `pending` rows and the session
update only `confirmed` ones, so a second run settles nothing and writes no
second event.

**How often it runs decides how stale an outcome is, and nothing more.** An
unsettled session stays `confirmed`, which keeps holding its slot exactly as it
did while it was upcoming, and stays out of `session_stats` because that reads
terminal statuses only. So a late run under-reports rather than misreports —
which is the right way round, and is why this needs no tight interval.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.infra.db.engine import resolve_async_dsn
from app.infra.db.session_writer import settle_attendance
from app.infra.etl.cli import EXIT_OK, configure_streams


async def run(args: argparse.Namespace) -> int:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with AsyncSession(engine) as session:
            settled = await settle_attendance(session, now=datetime.now(UTC))
            if args.dry_run:
                # Rolled back rather than skipped: a dry run that took a
                # different code path would report on a query nobody runs.
                await session.rollback()
                print(f"would settle {settled} session(s)")
            else:
                await session.commit()
                print(f"settled {settled} session(s)")
    finally:
        await engine.dispose()
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the count and roll back, changing nothing.",
    )
    args = parser.parse_args()
    configure_streams()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

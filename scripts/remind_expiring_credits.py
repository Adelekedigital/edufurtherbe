"""Nudge anybody sitting on credits that are about to run out.

    # report who would be nudged, writing nothing
    uv run python scripts/remind_expiring_credits.py --dry-run

    # queue the messages
    uv run python scripts/remind_expiring_credits.py

**Daily, and the cadence is free.** The sweep asks which reminders are *due now*
rather than which day it is, and `uq_outbox_events_reminder` refuses a second row
for a nudge already queued. So running twice in a day queues nothing extra, and
missing a day costs one nudge rather than duplicating another — which is the
property that lets this share a schedule with anything else without coordination.

**It queues; it does not send.** The outbox drain is what talks to Loops, so a
provider outage delays delivery rather than losing the message, and this script's
count is "how many people are owed a nudge" rather than "how many emails went".

Wiring only. No SQL and no business rule lives here: `scripts/` is checked by
ruff alone — not mypy, not bandit, and not by coverage (settled decision #44).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from app.core.config import get_settings
from app.infra.db.credit_reminders import (
    CREDIT_REMINDERS,
    expiring_soon,
    remind_about_expiring_credits,
)
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.etl.cli import EXIT_OK, configure_streams


async def run(args: argparse.Namespace) -> int:
    engine = create_database_engine(get_settings())
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            now = dt.datetime.now(dt.UTC)

            if args.dry_run:
                # **The same `expiring_soon` the real run calls**, per offset, so
                # the report cannot answer a different question from the sweep.
                for reminder in CREDIT_REMINDERS:
                    due = await expiring_soon(session, reminder, now=now)
                    print(f"{reminder.kind}: {len(due)} owed a nudge ({reminder.interval})")
                return EXIT_OK

            queued = await remind_about_expiring_credits(session, now=now)
            await session.commit()
            print(f"queued {queued} nudge(s)")
    finally:
        await engine.dispose()
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report who is due a nudge, changing nothing.",
    )
    args = parser.parse_args()
    configure_streams()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

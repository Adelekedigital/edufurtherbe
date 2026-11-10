"""Send the analytics history from before live events began (PRD 30 PR 7).

    railway run uv run python scripts/backfill_analytics.py            # dry run
    railway run uv run python scripts/backfill_analytics.py --send     # send

Run once, against a test PostHog project first. A dry run (the default) counts
the events per name and shows three built ones by id; ``--send`` sends them and
needs analytics configured. A stopped run picks up from ``--cursor``. The rules
are in `app.infra.analytics_backfill`; this only wires them up.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path

from app.core.config import get_settings
from app.infra.analytics_backfill import BackfillRefusedError, backfill_analytics, historical_sink
from app.infra.db.engine import create_database_engine
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams

#: In the working directory, and ignored by git.
DEFAULT_CURSOR = Path(".analytics-backfill-cursor.json")


def _instant(value: str) -> dt.datetime:
    at = dt.datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise argparse.ArgumentTypeError("--until needs a zone, e.g. 2026-11-10T00:00:00+00:00")
    return at


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        sink = historical_sink(settings) if args.send else None
        report = await backfill_analytics(engine, sink=sink, cursor=args.cursor, until=args.until)
    except BackfillRefusedError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED
    finally:
        await engine.dispose()
    print(f"cutoff: {report.cutoff.isoformat()}")
    for event, count in report.counts.items():
        print(f"{event}: {count}")
    for event in report.sample:
        print(f"sample: {event['event']} uuid={event['uuid']} distinct_id={event['distinct_id']}")
    print(f"sent: {report.sent}" if args.send else "dry run: nothing sent")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send; without it, a dry run.")
    parser.add_argument("--until", type=_instant, help="Cutoff instead of the first live event.")
    parser.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR, help="Resume file.")
    configure_streams()
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

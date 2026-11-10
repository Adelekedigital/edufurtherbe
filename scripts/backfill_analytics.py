"""Send the analytics history from before live events began (PRD 30 PR 7).

    railway run uv run python scripts/backfill_analytics.py --until <instant>          # dry run
    railway run uv run python scripts/backfill_analytics.py --until <instant> --send   # send

``--until`` is required and needs a zone: set it to the moment the server
events started being queued, i.e. the production deploy of the server-events
PR. Only facts strictly before it are sent; from then on each has its live
event. Keep the same value for every run, so a rerun builds the same events
with the same ids and PostHog drops the repeats. It is never read from the
outbox: those rows can be purged (`forget_unsent_analytics`), which would move
the cutoff and send facts twice under different ids.

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


def parser() -> argparse.ArgumentParser:
    built = argparse.ArgumentParser(description=__doc__)
    built.add_argument("--send", action="store_true", help="Send; without it, a dry run.")
    built.add_argument(
        "--until",
        type=_instant,
        required=True,
        help=(
            "Required, with a zone: when server events started being queued (the production "
            "deploy of the server-events PR). Only facts before it are sent. Use the same "
            "value on every run so reruns dedupe."
        ),
    )
    built.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR, help="Resume file.")
    return built


def main() -> int:
    configure_streams()
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

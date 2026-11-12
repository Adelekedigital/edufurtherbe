"""Give every PostHog person its properties once (decision 340).

    railway run uv run python scripts/sync_person_properties.py          # dry run
    railway run uv run python scripts/sync_person_properties.py --send   # send

Persons the history backfill created have none, and a backfill rerun cannot
add them (PostHog drops its repeated event ids). This sends one `$set` event
per live, non-internal account **PostHog already knows**, in batches; it never
creates a person, and is safe to rerun. Both modes need the private API
(`ANALYTICS_API_HOST`, `ANALYTICS_PROJECT_ID`, `ANALYTICS_API_KEY`) to ask
PostHog whom it knows. A dry run (the default) prints the counts and one
example payload. The rules are in `app.infra.analytics_person_sync`; this only
wires them up.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.config import get_settings
from app.core.errors import AnalyticsUpstreamError, UpstreamError
from app.infra.analytics_backfill import BackfillRefusedError, sending_sink
from app.infra.analytics_person_sync import SyncReport, sync_person_properties
from app.infra.clients.posthog_reader import NullAnalyticsReader, analytics_reader_for
from app.infra.db.engine import create_database_engine
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams

#: Stopped part way: rerun, the updates are safe to repeat.
EXIT_STOPPED = 3


def summary(report: SyncReport, *, send: bool) -> list[str]:
    """The counts a finished run prints."""
    lines = [
        f"sendable: {report.sendable}",
        f"known to PostHog: {report.known}",
        f"to send: {report.known}",
    ]
    if report.sample is not None:
        lines.append("sample: " + json.dumps(report.sample, sort_keys=True))
    lines.append(f"sent: {report.sent}" if send else "dry run: nothing sent")
    return lines


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    reader = analytics_reader_for(settings)
    if isinstance(reader, NullAnalyticsReader):
        print("refused: set ANALYTICS_API_HOST, ANALYTICS_PROJECT_ID and ANALYTICS_API_KEY")
        return EXIT_REFUSED
    engine = create_database_engine(settings)
    # Outside the `try`, so a stopped run still says how far it got.
    report = SyncReport()
    try:
        sink = sending_sink(settings, historical_migration=False) if args.send else None
        if args.send:
            print(f"host: {settings.analytics_host}")
        await sync_person_properties(engine, reader=reader, sink=sink, report=report)
    except BackfillRefusedError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED
    except (UpstreamError, AnalyticsUpstreamError) as exc:
        print(f"stopped after sending {report.sent}, rerun to finish: {exc}")
        return EXIT_STOPPED
    finally:
        await engine.dispose()
    for line in summary(report, send=args.send):
        print(line)
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    built = argparse.ArgumentParser(description=__doc__)
    built.add_argument("--send", action="store_true", help="Send; without it, a dry run.")
    return built


def main() -> int:
    configure_streams()
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

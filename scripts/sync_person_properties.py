"""Give every PostHog person its properties once (decision 340).

    railway run uv run python scripts/sync_person_properties.py          # dry run
    railway run uv run python scripts/sync_person_properties.py --send   # send

Persons the history backfill created have none, and a backfill rerun cannot
add them (PostHog drops its repeated event ids). This sends one `$set` event
per live, non-internal account, in batches; safe to rerun. A dry run (the
default) prints the count and one example payload. The rules are in
`app.infra.analytics_person_sync`; this only wires them up.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.config import get_settings
from app.core.errors import UpstreamError
from app.infra.analytics_backfill import BackfillRefusedError, sending_sink
from app.infra.analytics_person_sync import sync_person_properties
from app.infra.db.engine import create_database_engine
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams

#: Stopped part way: rerun, the updates are safe to repeat.
EXIT_STOPPED = 3


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        sink = sending_sink(settings, historical_migration=False) if args.send else None
        if args.send:
            print(f"host: {settings.analytics_host}")
        report = await sync_person_properties(engine, sink=sink)
    except BackfillRefusedError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED
    except UpstreamError as exc:
        print(f"stopped, rerun to finish: {exc}")
        return EXIT_STOPPED
    finally:
        await engine.dispose()
    print(f"persons: {report.built}")
    if report.sample is not None:
        print("sample: " + json.dumps(report.sample, sort_keys=True))
    print(f"sent: {report.sent}" if args.send else "dry run: nothing sent")
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

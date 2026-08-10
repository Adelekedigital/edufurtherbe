"""Transform a legacy snapshot into the availability tables, then report.

    # transform and report, touching no database
    uv run python scripts/load_availability.py --from-export script-data-dev --dry-run

    # load
    uv run python scripts/load_availability.py --from-export script-data-dev

**Run this after ``load_identity.py`` and ``load_profiles.py``.** Every rule
hangs off ``mentor_profiles``, so out of order it fails on a foreign key.

**The dry run is the point of this script, not a rehearsal of the write.** The
Gen A quarantine, the merged overlaps, the dropped rows and the mentors who must
re-declare their availability are all decided by the transform, which touches
nothing — so ``--dry-run`` produces the entire report without a database.

Wiring only. No SQL and no business rule lives here: ``scripts/`` is checked by
ruff alone — not mypy, not bandit, and not by coverage — so anything it contains
is unguarded (settled decision #44).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domain.bubble import EXPORT_TIMEZONE
from app.domain.transform.availability import AvailabilityPlan, plan_availability
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.availability import AvailabilityLoader
from app.infra.etl.cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNRESOLVED,
    configure_streams,
    open_export,
)
from app.infra.etl.satellites import user_id_map


def build_plan(directory: Path) -> AvailabilityPlan:
    source = open_export(directory)
    users = list(source.read("allusers"))
    settings_records = list(source.read("calendarsettings"))
    extra_records = list(source.read("calendarextra"))

    # Raw records in. Deciding what a user's timezone is, and who counts as a
    # mentor, are mapping decisions — and this directory is checked by ruff
    # alone, so a rule written here is a rule nothing verifies (#44).
    return plan_availability(
        users, settings_records, extra_records, export_timezone=EXPORT_TIMEZONE
    )


async def load(plan: AvailabilityPlan) -> None:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            # `user_id_map` rather than a query written here. The first draft of
            # this script carried its own copy of that SELECT, which was two
            # defects at once: SQL inside `scripts/`, where only ruff can see it
            # (#44), and a second representation of a statement that already
            # existed in `satellites.py` (#43).
            counts = await AvailabilityLoader(connection).load(
                users=await user_id_map(connection),
                rules=plan.rules,
                exceptions=plan.exceptions,
            )
        print(f"\nloaded {counts.rules} rules and {counts.exceptions} exceptions")
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-export", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    configure_streams()
    plan = build_plan(args.from_export)
    print(plan.report())

    if not plan.ok:
        print("\nrefusing to load: the transform reported errors")
        return EXIT_REFUSED
    # A run that loaded cleanly but left something for a human is not a clean
    # run, and `EXIT_UNRESOLVED` exists for exactly that. Returning 0 here
    # would let a freeze-window runbook read "quarantined 11" as success.
    unresolved = bool(plan.quarantined or plan.zone_defaults or plan.merged_overlaps)

    if args.dry_run:
        return EXIT_UNRESOLVED if unresolved else EXIT_OK

    asyncio.run(load(plan))
    return EXIT_UNRESOLVED if unresolved else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

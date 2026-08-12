"""Transform a legacy snapshot into the session tables, then report.

    # transform and report, touching no database
    uv run python scripts/load_sessions.py --from-export script-data-dev --dry-run

    # load
    uv run python scripts/load_sessions.py --from-export script-data-dev

**Run this after ``load_identity.py``, ``load_profiles.py`` and
``load_availability.py``.** Every session references two users, and every session
type references ``mentor_profiles``, so out of order it fails on a foreign key.

**The dry run carries the pre-flight, and that is what makes it worth running
early.** ``overlapping_live_windows`` lists mentor slots that the exclusion
constraint will refuse — and the only moment those can be fixed cheaply is while
Bubble is still writable. Finding them at load time means finding them inside the
freeze window. Expected result is zero; a non-zero one is a race or a frontend
bypass rather than dirty data (settled decision #84).

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
from app.domain.transform.sessions import SessionPlan, plan_sessions
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNRESOLVED,
    ReconciliationError,
    configure_streams,
    open_export,
)
from app.infra.etl.reconcile import reconcile_sessions
from app.infra.etl.satellites import user_id_map
from app.infra.etl.sessions import SessionLoader


def build_plan(directory: Path) -> SessionPlan:
    source = open_export(directory)
    users = list(source.read("allusers"))
    bookings = list(source.read("sessionbookings"))
    trackers = list(source.read("sessiontracker"))
    calendar = list(source.read("calendarsettings"))

    # Raw records in, and the zone from the source rather than a second constant
    # here. Deciding who counts as a mentor, which tracker belongs to which
    # booking, and what a mentor's session duration is are all mapping
    # decisions — and this directory is checked by ruff alone (#44).
    #
    # `calendarsettings` is read by this transform *and* by
    # `load_availability.py`, for different fields. That is deliberate and the
    # sessions module states it: Generation A rows are quarantined for their
    # times and still carry a usable `meetingDuration-TxT`.
    return plan_sessions(users, bookings, trackers, calendar, export_timezone=source.timezone)


async def load(plan: SessionPlan) -> None:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            # `user_id_map` rather than a query written here — SQL in `scripts/`
            # is unguarded, and a second copy of a statement that already exists
            # in `satellites.py` is decision #43.
            await SessionLoader(connection).load(users=await user_id_map(connection), plan=plan)
            # **Inside the transaction, and it raises.** Reconciling after a
            # commit reports a problem it is no longer able to undo.
            #
            # The loader returns nothing on purpose: what landed is read back
            # from the five tables by this call, not counted from what was
            # handed in.
            result = await reconcile_sessions(connection, plan)
            print()
            print(result.report())
            if not result.ok:
                raise ReconciliationError("reconciliation failed; the load has been rolled back")
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
    # run. Quarantined trackers are somebody's session history that did not
    # migrate; an overlapping window will abort the load outright; a defaulted
    # duration is a mentor whose offering was guessed at. Returning 0 would let a
    # freeze-window runbook read any of those as success.
    unresolved = bool(
        plan.quarantined
        or plan.dropped
        or plan.overlapping_live_windows
        or plan.duration_defaulted
        or plan.duration_disagreements
    )

    if args.dry_run:
        return EXIT_UNRESOLVED if unresolved else EXIT_OK

    try:
        asyncio.run(load(plan))
    except ReconciliationError as exc:
        print(f"\n{exc}")
        return EXIT_REFUSED

    return EXIT_UNRESOLVED if unresolved else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

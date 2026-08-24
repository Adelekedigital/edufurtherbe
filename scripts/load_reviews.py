"""Transform a legacy snapshot into the reviews table, then report.

    # transform and report, touching no database
    uv run python scripts/load_reviews.py --from-export script-data-dev --dry-run

    # load
    uv run python scripts/load_reviews.py --from-export script-data-dev

**Run this after ``load_identity.py``.** Every review references two users by
their Bubble anchors, so out of order it fails resolving them. It does *not*
depend on ``load_sessions.py``: every migrated review takes ``session_id = NULL``,
because the legacy type carries no session link and inventing one is
irreversible.

**The dry run is where the 53 rows get inspected**, and it is worth running the
moment the export exists rather than at cutover. The transform refuses a rating
that is not one of ``1.67 / 3.34 / 5``, so a value nobody predicted shows up here
— while Bubble is still writable — instead of inside the freeze window.

**A quarantined review is somebody's words that did not migrate.** The run exits
non-zero when there are any, so a clean-looking load cannot hide one.

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
from app.domain.transform.reviews import ReviewPlan, plan_reviews
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNRESOLVED,
    ReconciliationError,
    configure_streams,
    open_export,
)
from app.infra.etl.reconcile import reconcile_reviews
from app.infra.etl.reviews import ReviewLoader
from app.infra.etl.satellites import user_id_map


def build_plan(directory: Path) -> ReviewPlan:
    source = open_export(directory)
    # Raw records in, and the zone from the source rather than a second constant
    # here — deciding what a scaled rating means is a mapping decision, and this
    # directory is checked by ruff alone (#44).
    return plan_reviews(list(source.read("review")), timezone=source.timezone)


async def load(plan: ReviewPlan) -> None:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            # `user_id_map` rather than a query written here — SQL in `scripts/`
            # is unguarded, and a second copy of a statement that already exists
            # in `satellites.py` is decision #43.
            await ReviewLoader(connection).load(users=await user_id_map(connection), plan=plan)
            # **Inside the transaction, and it raises.** Reconciling after a
            # commit reports a problem it is no longer able to undo.
            result = await reconcile_reviews(connection, plan)
            print()
            print(result.report())
            if not result.ok:
                message = "reconciliation failed; the load has been rolled back"
                raise ReconciliationError(message)
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

    # **A run that leaves something for a human is not a clean run.** A
    # quarantined review is a mentee's words that did not migrate, and a
    # `Creator` disagreement is two source fields contradicting each other about
    # who wrote one. Returning 0 would let either scroll past in a cutover.
    unresolved = bool(plan.quarantined or plan.creator_mismatches)

    # **Computed before the dry-run branch, and that is the point.** The dry run
    # *is* the rehearsal for the 53 rows, run before the freeze while Bubble is
    # still writable — so it is the one moment the signal has to be trustworthy.
    # Returning 0 here regardless would report success on a run that quarantined
    # every row. Both sibling loaders return on this same expression.
    if args.dry_run:
        return EXIT_UNRESOLVED if unresolved else EXIT_OK

    try:
        asyncio.run(load(plan))
    except ReconciliationError as exc:
        # `EXIT_REFUSED`, not `EXIT_UNRESOLVED`: the reconciliation raises inside
        # the transaction, so nothing committed and the database is untouched —
        # which is exactly what 1 means and 2 does not. Both sibling loaders
        # return it from the identical handler.
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_REFUSED

    return EXIT_UNRESOLVED if unresolved else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

"""Transform a legacy snapshot into opening credit balances, then report.

    # transform and report, writing nothing
    uv run python scripts/load_credits.py --from-export script-data-dev --dry-run

    # load
    uv run python scripts/load_credits.py --from-export script-data-dev

**Run this after ``load_identity.py``.** Every balance names a user by their
Bubble anchor and the starter's condition is read from ``user_onboarding``, so
out of order it resolves nothing and reports every anchor missing.

**This dry run reads the database, where its siblings do not.** Whether somebody
gets a starter depends on ``user_onboarding.completed_at`` — the same fact
`grant_starter` keys on — and that lives in the table rather than in the export.
Deriving it from the raw field here would be a second answer to "has this person
finished", free to disagree with the first. Nothing is written either way: the
dry run opens a connection, reads two columns, and rolls back.

**The dry run is where the balances get inspected**, and it is worth running the
moment the export exists rather than at cutover. The transform refuses a value it
cannot read and anything above a plausible ceiling, so a `bookingCredit` nobody
predicted shows up here — while Bubble is still writable — instead of inside the
freeze window.

**A quarantined balance is somebody's credits that did not migrate.** The run
exits non-zero when there are any, so a clean-looking load cannot hide one.

Wiring only. No SQL and no business rule lives here: ``scripts/`` is checked by
ruff alone — not mypy, not bandit, and not by coverage — so anything it contains
is unguarded (settled decision #44).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domain.transform.credits import CreditPlan, plan_opening_balances
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNRESOLVED,
    ReconciliationError,
    configure_streams,
    open_export,
)
from app.infra.etl.credits import CreditLoader, finished_onboarding
from app.infra.etl.reconcile import reconcile_credits


async def build_plan(directory: Path, *, thing: str, cutover: dt.datetime) -> CreditPlan:
    """Read the export and the onboarding state, and decide.

    The connection is opened read-only in effect — `finished_onboarding` issues
    one `SELECT` — and closed before anything is written, so a dry run that
    stops here has touched nothing.
    """
    source = open_export(directory)
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.connect() as connection:
            finished = await finished_onboarding(connection)
    finally:
        await engine.dispose()

    return plan_opening_balances(list(source.read(thing)), cutover=cutover, finished=finished)


async def load(plan: CreditPlan) -> None:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            await CreditLoader(connection).load(plan)
            # **Inside the transaction, and it raises.** Reconciling after a
            # commit reports a problem it is no longer able to undo.
            result = await reconcile_credits(connection, plan)
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
    # **The same Thing `load_identity` was given.** That script takes it as a
    # required argument because the user Thing is the one whose name differs
    # between the dev app and production — planning against a file the identity
    # load never read would report every anchor missing.
    parser.add_argument("--thing", default="allusers", help="the user Thing")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    configure_streams()

    # **The wall clock, and it decides when every migrated credit dies.** The
    # expiry is end of the cutover month, so running this on 31 August and on
    # 1 September produce lots a month apart — which is a fact about the
    # migration rather than a knob, and is why the runbook schedules the load
    # rather than leaving it to whoever is awake.
    cutover = dt.datetime.now(dt.UTC)
    plan = asyncio.run(build_plan(args.from_export, thing=args.thing, cutover=cutover))
    print(plan.report())

    # **A run that leaves something for a human is not a clean run.** A
    # quarantined balance is somebody's credits that did not migrate, and
    # returning 0 would let it scroll past in a cutover.
    unresolved = bool(plan.quarantined)

    # **Computed before the dry-run branch, and that is the point.** The dry run
    # is the rehearsal, run before the freeze while Bubble is still writable, so
    # it is the one moment the signal has to be trustworthy. Every sibling
    # loader returns on this same expression.
    if args.dry_run:
        return EXIT_UNRESOLVED if unresolved else EXIT_OK

    try:
        asyncio.run(load(plan))
    except ReconciliationError as exc:
        # `EXIT_REFUSED`, not `EXIT_UNRESOLVED`: the reconciliation raises inside
        # the transaction, so nothing committed and the database is untouched —
        # which is exactly what 1 means and 2 does not.
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_REFUSED

    return EXIT_UNRESOLVED if unresolved else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

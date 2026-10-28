"""Transform the legacy chats into conversations and messages, then report.

    # transform and report, touching no database
    uv run python scripts/load_messages.py --from-export script-data-dev --dry-run

    # load
    uv run python scripts/load_messages.py --from-export script-data-dev

Reads ``messagestarter.json`` and ``messagethread.json`` (Data-tab exports).
**Run this after ``load_identity.py``** (every row names users by their Bubble
anchors) **and after ``load_sessions.py``**: the newest booking between the two
people decides which of them is the mentor (decision 302).

**A quarantined row is somebody's words that did not migrate**, so the run
exits non-zero when there are any. A chat whose mentor nothing can name stops
the load with the chat named, and nothing is written.

Wiring only. No SQL and no business rule lives here: ``scripts/`` is checked by
ruff alone (settled decision #44).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domain.transform.messages import MessagePlan, plan_messages
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNRESOLVED,
    ReconciliationError,
    configure_streams,
    open_export,
)
from app.infra.etl.messages import MessageLoader
from app.infra.etl.reconcile import reconcile_messages
from app.infra.etl.satellites import user_id_map


def build_plan(directory: Path) -> MessagePlan:
    source = open_export(directory)
    return plan_messages(
        list(source.read("messagestarter")),
        list(source.read("messagethread")),
        timezone=source.timezone,
    )


async def load(plan: MessagePlan) -> None:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            # `user_id_map` rather than a query written here — SQL in `scripts/`
            # is unguarded, and a second copy of a statement that already exists
            # in `satellites.py` is decision #43.
            await MessageLoader(connection).load(users=await user_id_map(connection), plan=plan)
            # **Inside the transaction, and it raises.** Reconciling after a
            # commit reports a problem it is no longer able to undo.
            result = await reconcile_messages(connection, plan)
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

    # A quarantined row is a message or a chat that did not migrate; the dry
    # run is the rehearsal, so it returns on the same signal as the load.
    unresolved = bool(plan.quarantined)

    if args.dry_run:
        return EXIT_UNRESOLVED if unresolved else EXIT_OK

    try:
        asyncio.run(load(plan))
    except (ReconciliationError, LookupError) as exc:
        # Raised inside the transaction, so nothing committed: a chat whose
        # mentor nothing names, an unknown user, or a failed reconciliation.
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_REFUSED

    return EXIT_UNRESOLVED if unresolved else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

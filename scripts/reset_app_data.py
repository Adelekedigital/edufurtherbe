"""Clear rehearsal app data before the real load (cutover page, 18 Nov 2026).

    uv run python scripts/reset_app_data.py --confirm-database <project ref>
    uv run python scripts/reset_app_data.py --confirm-database <project ref> --apply

A **dry run by default**: prints the target, every kept table and every table
it would clear with row counts, and writes nothing. ``--apply`` deletes, in one
transaction, and commits only if every cleared table is empty and every kept
table is unchanged; then it removes the private files (intake and message
uploads) the cleared rows named from the intake bucket.

**It refuses, writing nothing, once real data exists**: an account that signed
up on the new app, or a row created at or after ``--real-data-from`` (default:
the start of the final load window; the flag may only move it earlier). There
is no flag that skips that check. ``--confirm-database`` must name the
connected database: the Supabase project ref, or the database name elsewhere.

Then reload with the loaders, in the runbook's order, and run
``provision_auth.py``: existing sign-ins relink by address.

Exit codes: 0 done (dry run or applied), 1 refused and nothing written,
2 applied but some private files could not be removed (their keys are listed).

Wiring only. The rules are ``app/domain/app_reset.py`` and the SQL is
``app/infra/etl/reset.py`` (settled decision #44).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

import httpx

from app.core.config import get_settings
from app.domain.app_reset import DEFAULT_REAL_DATA_FROM
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.cli import configure_streams
from app.infra.etl.reset import run
from app.infra.storage.supabase import intake_storage_for

STORAGE_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-database", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--real-data-from",
        type=dt.datetime.fromisoformat,
        default=DEFAULT_REAL_DATA_FROM,
        help="rows created at or after this zoned instant are real data (earlier only)",
    )
    args = parser.parse_args()

    configure_streams()
    settings = get_settings()
    with httpx.Client(timeout=STORAGE_TIMEOUT) as client:
        storage = intake_storage_for(settings, client)
        return asyncio.run(
            run(
                resolve_async_dsn(settings),
                confirm=args.confirm_database,
                apply=args.apply,
                real_data_from=args.real_data_from,
                remove_file=storage.delete if storage else None,
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())

"""Grant this month's credits to every unlocked mentee.

Run on the 1st by `.github/workflows/monthly-credits.yml`, and by hand when
somebody wants it now.

**Safe to run repeatedly, which it will be.** The grant is guarded by
`uq_credit_lots_one_monthly_grant_per_period`, so a second run in the same month
inserts nothing and reports zero. That matters more here than for the hourly
sweep: this job runs twelve times a year, so a mistake has eleven months to sit
unnoticed, and the retry that would expose it is the same retry that would
double everybody's balance without the guard.

**Late is safe; early is not.** The period comes from the expiry the grant
computes, not from the day the job runs, so a job that fires on the 3rd still
belongs to that month. Running *before* the 1st would grant next month's credits
early — which the schedule prevents and nothing here does, and is worth knowing
before somebody triggers it manually to test.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from app.core.config import get_settings
from app.infra.db.credit_grants import grant_monthly_credits, unlocked_mentee_count
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.etl.cli import EXIT_OK, configure_streams


async def run(args: argparse.Namespace) -> int:
    engine = create_database_engine(get_settings())
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            now = dt.datetime.now(dt.UTC)

            if args.dry_run:
                # **Reported from the same two `EXISTS` the grant uses**, not a
                # second predicate that could drift. A dry run answering a
                # different question from the real run is worse than no dry run.
                eligible = await unlocked_mentee_count(session)
                print(f"would grant {eligible} unlocked mentee(s)")
                return EXIT_OK

            granted = await grant_monthly_credits(session, now=now)
            await session.commit()
            print(f"granted {granted} mentee(s)")
    finally:
        await engine.dispose()
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many would be granted, changing nothing.",
    )
    args = parser.parse_args()
    configure_streams()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

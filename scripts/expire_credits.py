"""Retire every credit lot whose date has passed, and record what it lost.

Run on the 1st by `.github/workflows/expire-credits.yml`, an hour before the
monthly grant, and by hand whenever somebody wants the ledger caught up.

**Nothing here changes a balance.** The balance already stopped counting these
lots the moment their date passed — `spendable_now` decides that on every read,
without asking whether this job ran. What this writes is the ledger row that
says *why* the number fell, which is the whole of D8's argument against a
counter. So a run that is late is a ledger that is late; a run that never
happens is a ledger with a hole in it and a balance that is still correct.

**Safe to run repeatedly**, and not because of an index. The sweep only touches
lots with credits left and sets them to zero, so a second run finds nothing and
reports zero. That is self-terminating rather than guarded, which is why this
job ships without a migration where the monthly grant needed one.

**Early is the dangerous direction, as it is for the grant** — and here it is
harmless rather than merely prevented. Running on 31 August finds nothing: the
August lots die at midnight on the 1st, and until then `spendable_now` says they
are alive and this sweep agrees, because it asks the same question.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from app.core.config import get_settings
from app.infra.db.credit_expiry import expirable_credit_count, expire_credits
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.etl.cli import EXIT_OK, configure_streams


async def run(args: argparse.Namespace) -> int:
    engine = create_database_engine(get_settings())
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            now = dt.datetime.now(dt.UTC)

            if args.dry_run:
                # **The same `_dead` the sweep uses**, not a second predicate.
                # A dry run answering a different question from the real run is
                # worse than no dry run.
                expirable = await expirable_credit_count(session, now=now)
                print(f"would expire {expirable} lot(s)")
                return EXIT_OK

            expired = await expire_credits(session, now=now)
            await session.commit()
            print(f"expired {expired} lot(s)")
    finally:
        await engine.dispose()
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many lots would be retired, changing nothing.",
    )
    args = parser.parse_args()
    configure_streams()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

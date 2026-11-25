"""Top every mentee up to their credit level for this month, now (decision 370).

    railway run uv run python scripts/top_up_levels.py           # dry run: who and how much
    railway run uv run python scripts/top_up_levels.py --apply   # pay it

When the level rule changes mid-month, the 1st pays everyone their new level
anyway; this pays it at once. Nothing here decides anything: who is owed and
how much is `owed_top_ups`, the same arithmetic the 1st and every top-up use,
and the payment is `top_up_everyone`, under the same per-user locks.

**Safe to re-run**: a month already at its level owes nothing, spent or not.
Silent, as the top-up at a settlement is. One transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections import Counter
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.domain.credits import credit_ladder
from app.infra.db.credit_grants import owed_top_ups, top_up_everyone
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.etl.cli import EXIT_OK, configure_streams


def _summary(paid: Mapping[UUID, int]) -> str:
    by_amount = Counter(paid.values())
    parts = ", ".join(f"{n} owed {amount}" for amount, n in sorted(by_amount.items()))
    return f"{len(paid)} mentees, {sum(paid.values())} credits" + (f" ({parts})" if parts else "")


async def run(*, apply: bool, engine: AsyncEngine, settings: Settings, now: dt.datetime) -> int:
    ladder = credit_ladder(settings)
    async with create_session_factory(engine)() as session:
        if not apply:
            print(f"owed now: {_summary(await owed_top_ups(session, now=now, ladder=ladder))}")
            print("dry run: nothing paid")
            return EXIT_OK
        paid = await top_up_everyone(session, now=now, ladder=ladder)
        await session.commit()
    print(f"paid: {_summary(paid)}")
    return EXIT_OK


async def _main(*, apply: bool, settings: Settings) -> int:
    engine = create_database_engine(settings)
    try:
        return await run(apply=apply, engine=engine, settings=settings, now=dt.datetime.now(dt.UTC))
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Pay it; default is a dry run.")
    args = parser.parse_args()
    configure_streams()
    return asyncio.run(_main(apply=args.apply, settings=get_settings()))


if __name__ == "__main__":
    raise SystemExit(main())

"""Give every live account without a public handle one (owner, 2026-11-14).

    railway run uv run python scripts/backfill_slugs.py            # dry run
    railway run uv run python scripts/backfill_slugs.py --apply    # write

From the name where there is one, else a ``user-…`` placeholder that the
first name save replaces. Existing handles, imported ones included, are never
touched. Safe to re-run: a second run finds nothing to do. The rules are in
`app.domain.slugs` and `app.infra.db.slugs`; this only wires them up.

Exit 2 when an existing handle does not fit the frontend's handle pattern:
a person decides what to do about it.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.db.engine import create_database_engine
from app.infra.db.slugs import backfill_slugs
from app.infra.etl.cli import EXIT_OK, EXIT_UNRESOLVED, configure_streams


async def run(apply: bool) -> int:
    engine = create_database_engine(get_settings())
    try:
        async with AsyncSession(engine) as session:
            result = await backfill_slugs(session)
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    print(f"handles from names   {result.from_name}")
    print(f"placeholder handles  {result.placeholders}")
    print(f"handles the frontend would refuse: {len(result.off_pattern)}")
    for slug in result.off_pattern:
        print(f"   {slug}")
    print("written" if apply else "dry run: nothing written")
    return EXIT_UNRESOLVED if result.off_pattern else EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write; without it, a dry run.")
    configure_streams()
    return asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(main())

"""Give the legacy load's untouched default offerings the default form and
name (decision 367).

    railway run uv run python scripts/adopt_default_form.py           # dry run
    railway run uv run python scripts/adopt_default_form.py --apply   # write

An offering qualifies while it is live, still named "General Mentorship", has
an empty description and has never had a question (`untouched_defaults`).
Each one is renamed to `DEFAULT_SESSION_TYPE_NAME` and given `DEFAULT_FORM`;
duration and description are kept. Each commits on its own, and is checked
again under a lock, so a mentor editing meanwhile wins. Safe to re-run: an
adopted offering no longer qualifies.

A dry run prints the count and ids only. ``--apply`` prints what happened to
each, and exits 1 if any was skipped because its mentor already has an
offering with the new name, which needs a person to decide.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.infra.db.default_form import adopt_default_form, untouched_defaults
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams


async def run(*, apply: bool, engine: AsyncEngine) -> int:
    sessions = create_session_factory(engine)
    async with sessions() as session:
        due = await untouched_defaults(session)
    print(f"untouched defaults: {len(due)}")
    for type_id in due:
        print(f"  {type_id}")
    if not apply:
        print("dry run: nothing changed")
        return EXIT_OK
    outcomes: Counter[str] = Counter()
    for type_id in due:
        async with sessions() as session:
            outcome = await adopt_default_form(session, type_id)
            await session.commit()
        outcomes[outcome] += 1
        if outcome != "adopted":
            print(f"  {type_id}: {outcome}")
    print(", ".join(f"{name}: {outcomes[name]}" for name in ("adopted", "name_taken", "changed")))
    return EXIT_REFUSED if outcomes["name_taken"] else EXIT_OK


async def _main(*, apply: bool) -> int:
    engine = create_database_engine(get_settings())
    try:
        return await run(apply=apply, engine=engine)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write; default is a dry run.")
    args = parser.parse_args()
    configure_streams()
    return asyncio.run(_main(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())

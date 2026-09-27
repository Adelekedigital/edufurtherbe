"""Find the face in every avatar stored before focal points existed.

    railway run uv run python scripts/backfill_avatar_focus.py

Safe to re-run and safe in any environment: it only writes an avatar's focal
point, only where none was ever decided, and never over a mentor's own choice or
a photo that changed while it ran. The loop and its rules are in
`app.infra.db.avatar_focus_store`; this only wires them up.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_storage
from app.core.config import get_settings
from app.infra.db.avatar_focus_store import backfill_avatar_focus
from app.infra.db.engine import create_database_engine
from app.infra.etl.cli import EXIT_OK, configure_streams


async def run() -> int:
    engine = create_database_engine(get_settings())
    try:
        async with AsyncSession(engine) as session:
            counts = await backfill_avatar_focus(session, get_storage())
    finally:
        await engine.dispose()
    print(f"avatar focus backfill: {counts}")
    return EXIT_OK


def main() -> int:
    configure_streams()
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())

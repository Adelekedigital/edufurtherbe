"""Sync the PostHog dashboards defined in the repo (decision 341).

    railway run uv run python scripts/sync_posthog_dashboards.py           # dry run
    railway run uv run python scripts/sync_posthog_dashboards.py --apply   # write

A dry run reads PostHog and the internal accounts and prints the plan: create,
update or remove per dashboard and insight, and the internal count.
``--apply`` makes it so, rewriting every managed item from the repo. Rerun
it after an account becomes internal or stops being so: the ids are refreshed.
Needs `ANALYTICS_API_HOST`, `ANALYTICS_PROJECT_ID` and `ANALYTICS_API_KEY`. A
failure prints what was already written, then one line, and exits non-zero.
The rules are in `app.infra.posthog_dashboards`; this only wires them up.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConfigurationError, UpstreamError
from app.infra.clients.posthog_admin import PostHogAdmin, posthog_admin_for
from app.infra.db.analytics_scope import internal_ids
from app.infra.db.engine import create_database_engine
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams
from app.infra.posthog_dashboards.sync import Plan, sync


async def read_internal() -> list[UUID]:
    engine = create_database_engine(get_settings())
    try:
        async with AsyncSession(engine) as session:
            return await internal_ids(session)
    finally:
        await engine.dispose()


def report(plan: Plan, apply: bool) -> list[str]:
    lines = [f"{c.kind} {c.action}: {c.name}" for c in plan.changes]
    lines.append(f"internal accounts left out of every insight: {plan.internal}")
    lines.append("applied" if apply else "dry run: nothing written")
    return lines


#: What a run reports as one line: a refusal, a database or network failure,
#: or an answer not shaped as documented.
FAILURES = (
    ConfigurationError,
    UpstreamError,
    SQLAlchemyError,
    OSError,
    AttributeError,
    KeyError,
    TypeError,
    ValueError,
)


def run(apply: bool, admin: PostHogAdmin | None = None) -> int:
    try:
        admin = admin or posthog_admin_for(get_settings())
        plan = sync(admin, asyncio.run(read_internal()), apply=apply)
    except FAILURES as exc:
        written = admin.written if admin is not None else {}
        done = ", ".join(f"{kind} {n}" for kind, n in sorted(written.items())) or "nothing"
        print(f"written before the failure: {done}")
        # One line: httpx puts a "for more information" line in its message.
        print(f"failed: {type(exc).__name__}: " + " ".join(str(exc).split()))
        return EXIT_REFUSED
    for line in report(plan, apply):
        print(line)
    return EXIT_OK


def main() -> int:
    configure_streams()
    parsed = argparse.ArgumentParser(description=__doc__)
    parsed.add_argument("--apply", action="store_true", help="Write; without it, a dry run.")
    return run(parsed.parse_args().apply)


if __name__ == "__main__":
    raise SystemExit(main())

"""Queue the mentee bio draft for every finished mentee whose bio is still
unwritten (decision 269's draft, backfilled; owner, 2026-11-25).

    railway run uv run python scripts/queue_bio_drafts.py           # dry run: the count
    railway run uv run python scripts/queue_bio_drafts.py --apply   # queue them

Drafts are queued at a mentee's first completion in the new app, so mentees
who finished in the legacy app never got one. This queues each of them to the
**same callback** onboarding queues (`/api/v1/callbacks/bio-drafts`), with the
same body (the user id only), so the prompt, the inputs sent to the router and
every guard are the callback's: it re-checks that the account is live, a mentee
and still without a bio, and the save writes only into an unwritten bio,
recording `about_me_drafted_at` so the bio reads as a draft. Nothing here
decides anything: the selection is `owed_bio_drafts`.

**Safe to re-run.** A second run lists only those still owed, and a duplicate
delivery finds the bio written and saves nothing. Deliveries are spread
`SPACING` apart so the router is not asked for them all at once.

``--apply`` refuses unless drafting is on, the assistant is configured, and
QStash and the public address are set: any missing one would queue drafts the
callback then skips.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps.bio_draft import BIO_DRAFT_CALLBACK_PATH
from app.core.config import Settings, get_settings
from app.domain.writing_assist import bio_draft_enabled
from app.infra.clients.ai_router import writing_assistant_for
from app.infra.clients.scheduler import (
    NullScheduler,
    SchedulerError,
    SchedulerPort,
    callback_destination,
    scheduler_for,
)
from app.infra.db.bio_draft_store import owed_bio_drafts
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams

#: Between two deliveries: 100 drafts take about 17 minutes.
SPACING = dt.timedelta(seconds=10)


def missing(settings: Settings) -> list[str]:
    """What is not configured, by setting name. Empty when all is in place."""
    gaps = []
    if not bio_draft_enabled(settings):
        gaps.append("MENTEE_BIO_DRAFT_ENABLED")
    if not writing_assistant_for(settings).available:
        gaps.append("WRITING_ASSIST_ENABLED + AI_ROUTER_URL + AI_ROUTER_PRIVATE_KEY")
    if isinstance(scheduler_for(settings), NullScheduler):
        gaps.append("QSTASH_TOKEN")
    if not settings.public_base_url:
        gaps.append("PUBLIC_BASE_URL")
    return gaps


async def run(
    *,
    apply: bool,
    engine: AsyncEngine,
    scheduler: SchedulerPort,
    callback_url: str,
    now: dt.datetime,
    spacing: dt.timedelta = SPACING,
) -> int:
    async with create_session_factory(engine)() as session:
        owed = await owed_bio_drafts(session)
    print(f"owed a draft: {len(owed)}")
    if not apply:
        print("dry run: nothing queued")
        return EXIT_OK
    failed = 0
    for index, user_id in enumerate(owed):
        try:
            scheduler.schedule(
                url=callback_url, body={"user_id": str(user_id)}, at=now + index * spacing
            )
        except SchedulerError:
            failed += 1
    print(f"queued: {len(owed) - failed}, failed to queue: {failed}")
    if failed:
        print("re-run to queue the rest: those already drafted are no longer listed")
    return EXIT_REFUSED if failed else EXIT_OK


async def _main(*, apply: bool, settings: Settings) -> int:
    engine = create_database_engine(settings)
    try:
        return await run(
            apply=apply,
            engine=engine,
            scheduler=scheduler_for(settings),
            callback_url=callback_destination(settings.public_base_url, BIO_DRAFT_CALLBACK_PATH)
            or "",
            now=dt.datetime.now(dt.UTC),
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Queue them; default is a dry run.")
    args = parser.parse_args()
    configure_streams()
    settings = get_settings()
    if args.apply and (gaps := missing(settings)):
        print(f"refusing: not configured: {', '.join(gaps)}")
        return EXIT_REFUSED
    return asyncio.run(_main(apply=args.apply, settings=settings))


if __name__ == "__main__":
    raise SystemExit(main())

"""Confirm, in the new app, the upcoming sessions the legacy load brought in
(decision 366).

    railway run uv run python scripts/confirm_migrated_sessions.py           # dry run
    railway run uv run python scripts/confirm_migrated_sessions.py --apply   # confirm

The load writes a confirmed session but none of what a confirmation does, so
each one is put through `on_confirmed`, the same step a booking or `/accept`
runs: its own EduFurther video room (or the venue its offering names now), a
calendar invite to both parties linking the session page, and the four
reminders. Nothing here decides anything: the selection is
`migrated_to_confirm` and the steps are `on_confirmed`.

A dry run (the default) lists the sessions by start time and id, nothing else.
``--apply`` refuses unless rooms, the calendar, QStash and both public
addresses are configured, because any missing one would confirm a session with
no room, no invite or no reminders and drop it from every later run. Each
session commits on its own, after its legacy venue (a mentor's personal room,
reused across sessions) is cleared. The run then names any session left
without an invite or a room and exits 1; fix those by hand rather than rerun,
because a room made under the session's name cannot be made twice. Google and
QStash are called before the commit, so a commit that fails leaves an invite
behind: delete that event before any rerun. Run it once, from one terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.domain.notifications import session_reminders_for
from app.infra.clients.meetings import NullCalendar, NullRooms, calendar_for, rooms_for
from app.infra.clients.scheduler import NullScheduler, callback_destination, scheduler_for
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.session_writer import (
    REMINDER_CALLBACK_PATH,
    FollowUps,
    clear_legacy_venue,
    confirmation_gaps,
    migrated_to_confirm,
    on_confirmed,
)
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams


def follow_ups_for(settings: Settings) -> FollowUps:
    """The adapters a confirmation uses, chosen as the API chooses them."""
    return FollowUps(
        rooms=rooms_for(settings),
        calendar=calendar_for(settings),
        scheduler=scheduler_for(settings),
        callback_url=callback_destination(settings.public_base_url, REMINDER_CALLBACK_PATH),
        app_base_url=settings.app_base_url or "",
    )


def missing(follow_ups: FollowUps) -> list[str]:
    """What is not configured, by setting name. Empty when all is in place."""
    gaps = []
    if isinstance(follow_ups.rooms, NullRooms):
        gaps.append("DAILY_API_KEY")
    if isinstance(follow_ups.calendar, NullCalendar):
        gaps.append("GOOGLE_OAUTH_CLIENT_ID/SECRET + GOOGLE_CALENDAR_REFRESH_TOKEN")
    if follow_ups.scheduler is None or isinstance(follow_ups.scheduler, NullScheduler):
        gaps.append("QSTASH_TOKEN")
    if not follow_ups.callback_url:
        gaps.append("PUBLIC_BASE_URL")
    if not follow_ups.app_base_url:
        gaps.append("APP_BASE_URL")
    return gaps


async def run(*, apply: bool, engine: AsyncEngine, follow_ups: FollowUps) -> int:
    sessions = create_session_factory(engine)
    now = dt.datetime.now(dt.UTC)
    async with sessions() as session:
        due = await migrated_to_confirm(session, now=now)
    print(f"to confirm: {len(due)}")
    for session_id, starts_at in due:
        print(f"  {starts_at.isoformat()}  {session_id}")
    if not apply:
        print("dry run: nothing changed")
        return EXIT_OK
    short: list[tuple[UUID, str]] = []
    for session_id, starts_at in due:
        async with sessions() as session:
            await clear_legacy_venue(session, session_id, now=now)
            published = await on_confirmed(session, session_id, follow_ups, now=now)
            await session.commit()
        owed = len(session_reminders_for(starts_at, now=now))
        if published < owed:
            short.append((session_id, f"{published} of {owed} reminders scheduled"))
    async with sessions() as session:
        gaps = await confirmation_gaps(session, [session_id for session_id, _ in due]) + short
    print(f"confirmed: {len(due)}, incomplete: {len({session_id for session_id, _ in gaps})}")
    for session_id, gap in gaps:
        print(f"  {session_id}: {gap}")
    return EXIT_REFUSED if gaps else EXIT_OK


async def _main(*, apply: bool, settings: Settings, follow_ups: FollowUps) -> int:
    engine = create_database_engine(settings)
    try:
        return await run(apply=apply, engine=engine, follow_ups=follow_ups)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Confirm them; default is a dry run.")
    args = parser.parse_args()
    configure_streams()
    settings = get_settings()
    follow_ups = follow_ups_for(settings)
    if args.apply and (gaps := missing(follow_ups)):
        print(f"refusing: not configured: {', '.join(gaps)}")
        return EXIT_REFUSED
    return asyncio.run(_main(apply=args.apply, settings=settings, follow_ups=follow_ups))


if __name__ == "__main__":
    raise SystemExit(main())

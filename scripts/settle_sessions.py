"""Advance every session whose deadline has passed.

    # advance what is due, and report how many
    uv run python scripts/settle_sessions.py

    # report what would move, touching nothing
    uv run python scripts/settle_sessions.py --dry-run

**Four sweeps, one job.** An unanswered *request* past ``respond_by`` becomes
``expired``; a *confirmed* session past its join window becomes ``completed`` or
``no_show``; and whatever either of them queued gets sent. The first two act on
disjoint statuses so their order does not matter, and the drain runs **last**
deliberately — a message queued by this run goes out in this run rather than
waiting an hour for the next one.

They are one script because they are one kind of thing: work nobody is waiting
on a request for. Three scripts would mean three schedules, three workflows and
three places to notice a failure.

**The expiry half is the one that frees a slot.**
``sessions_no_mentor_double_booking`` covers ``pending_mentor_approval``, so
until this runs an abandoned request holds the mentor's hour indefinitely.

**A script rather than a scheduled job inside the application**, because settled
decision #13 uses no platform-native queue or cron: the escape from FastAPI Cloud
stays real only if nothing depends on its scheduler. An external timer runs this,
exactly as the weekly institution mirror is run.

**Safe to run twice, and it will be.** An external scheduler is the kind that
fires twice — the participant update touches only `pending` rows and the session
update only `confirmed` ones, so a second run settles nothing and writes no
second event.

**How often it runs decides how stale an outcome is, and nothing more.** An
unsettled session stays `confirmed`, which keeps holding its slot exactly as it
did while it was upcoming, and stays out of `session_stats` because that reads
terminal statuses only. So a late run under-reports rather than misreports.

For the expiry half a late run costs the mentee a slot that is free but still
hidden, for at most one interval — an hour on the current schedule, against a
response window of six. Running *early* is the direction that would do damage,
and neither sweep can: both boundaries come from the data rather than from the
schedule.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.infra.clients.meetings import GoogleCalendar, NullCalendar, free_busy
from app.infra.clients.notifications import LoopsNotifier, NullNotifier
from app.infra.clients.templates import LoopsTemplates
from app.infra.db.calendar_store import check_connections
from app.infra.db.engine import resolve_async_dsn
from app.infra.db.outbox import drain
from app.infra.db.session_writer import expire_requests, remind_unreviewed, settle_attendance
from app.infra.etl.cli import EXIT_OK, configure_streams


def _notifier() -> LoopsNotifier | NullNotifier:
    """The real sender when a key is configured, and a loud nothing otherwise.

    Unset is not an error: the outbox still fills and drains, and every message
    is recorded as skipped rather than lost — so a missing key is visible in a
    table rather than in nobody's inbox.
    """
    settings = get_settings()
    key = settings.loops_api_key
    if key is None:
        return NullNotifier()
    # **Discovery is wired here and nowhere on the booking path.** Asking Loops
    # what a template declares is a network call; on a sweep that already runs
    # on a schedule it is free, and on `POST /sessions` it would mean a booking
    # cannot be made while Loops is slow.
    return (
        LoopsNotifier(key.get_secret_value())
        .with_settings(settings)
        .with_templates(LoopsTemplates(key.get_secret_value()))
    )


def _calendar() -> GoogleCalendar | NullCalendar:
    """The real calendar when all three values are configured, else a null one.

    All three or none: a refresh token is useless without the client that minted
    it, and two of three is the shape most likely to be a half-finished setup.
    """
    settings = get_settings()
    if not (
        settings.google_oauth_client_id
        and settings.google_oauth_client_secret
        and settings.google_calendar_refresh_token
    ):
        return NullCalendar()
    return GoogleCalendar(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret.get_secret_value(),
        refresh_token=settings.google_calendar_refresh_token.get_secret_value(),
        calendar_id=settings.google_calendar_id,
    )


def _calendar_health() -> dict[str, str] | None:
    """The mentor-facing OAuth client, or ``None`` when it is not configured.

    All three or none, matching `_calendar` above and `_free_busy` in `deps`:
    a deployment part-way through being set up should behave like one that has
    not started, rather than failing a sweep that has three other jobs to do.
    """
    settings = get_settings()
    if not (
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
        and settings.calendar_token_key
    ):
        return None
    return {
        "client_id": settings.google_calendar_client_id,
        "client_secret": settings.google_calendar_client_secret.get_secret_value(),
        "key": settings.calendar_token_key.get_secret_value(),
    }


async def run(args: argparse.Namespace) -> int:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with AsyncSession(engine) as session:
            now = datetime.now(UTC)
            # One clock for both sweeps. Reading it twice would let a session
            # sit exactly on a boundary and be judged by two different instants.
            expired = await expire_requests(session, now=now, calendar=_calendar())
            settled = await settle_attendance(session, now=now)
            # **Fourth, and after the settlement**, so a session that finished a
            # day ago and was settled in an earlier run is nudged here. It reads
            # the request rather than the settlement, so a suppressed request is
            # never nudged about.
            nudged = await remind_unreviewed(session, now=now)
            # **Third, and before the drain**, so a mentor whose calendar died
            # is told in this run rather than an hour later. It is the only
            # sweep here that talks to a third party to decide anything, which
            # is why it counts unreachable separately from disconnected: one is
            # a fact about a grant, the other about an afternoon.
            oauth = _calendar_health()
            health = (
                {"checked": 0, "healthy": 0, "disconnected": 0, "unreachable": 0}
                if oauth is None
                else await check_connections(session, now=now, reader=free_busy, **oauth)
            )
            # **The null notifier on a dry run, always.** A dry run rolls the
            # database back and cannot roll back an email, so sending for real
            # and then pretending nothing happened would be the one irreversible
            # thing this flag exists to avoid.
            sent = await drain(
                session,
                notifier=NullNotifier() if args.dry_run else _notifier(),
                now=now,
            )
            if args.dry_run:
                # Rolled back rather than skipped: a dry run that took a
                # different code path would report on a query nobody runs.
                await session.rollback()
                print(
                    f"would expire {expired} request(s), settle {settled} session(s), "
                    f"nudge {nudged} review(s), "
                    f"disconnect {health['disconnected']} calendar(s), "
                    f"send {sum(sent.values())} message(s)"
                )
            else:
                await session.commit()
                print(
                    f"expired {expired} request(s), settled {settled} session(s), "
                    f"nudged {nudged} review(s), calendars {health}, messages {sent}"
                )
    finally:
        await engine.dispose()
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the count and roll back, changing nothing.",
    )
    args = parser.parse_args()
    configure_streams()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

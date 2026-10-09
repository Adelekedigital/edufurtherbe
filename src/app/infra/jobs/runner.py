"""One dispatcher for recurring runtime work, independent of its trigger."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.domain.availability import booking_window
from app.domain.credits import credit_ladder
from app.domain.institutions import CatalogueError, CatalogueRow, to_catalogue_row
from app.infra.clients.hipolabs import FileCatalogue, HipolabsCatalogue
from app.infra.clients.meetings import (
    DailyRooms,
    GoogleCalendar,
    NullCalendar,
    NullRooms,
    free_busy,
)
from app.infra.clients.notifications import LoopsNotifier, NullNotifier, notifier_for
from app.infra.db.calendar_store import check_connections, free_busy_reader
from app.infra.db.credit_expiry import expirable_credit_count, expire_credits
from app.infra.db.credit_grants import grant_monthly_credits, unlocked_mentee_count
from app.infra.db.credit_reminders import (
    CREDIT_REMINDERS,
    expiring_soon,
    remind_about_expiring_credits,
)
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.idempotency import sweep_expired_keys
from app.infra.db.intake_file_store import sweep_counts, sweep_intake_files
from app.infra.db.mentor_status_store import remind_returning_mentors
from app.infra.db.next_available_store import refresh_next_available
from app.infra.db.outbox import drain
from app.infra.db.session_type_store import finalise_scheduled_deletions
from app.infra.db.session_writer import (
    confirm_presence,
    expire_requests,
    remind_unreviewed,
    settle_attendance,
)
from app.infra.db.triggers import timestamps_from_source_across
from app.infra.etl.institutions import country_ids, link_education, mirror
from app.infra.jobs.manifest import RUNTIME_JOB_NAMES
from app.infra.storage.supabase import intake_storage_for

logger = logging.getLogger(__name__)
INSTITUTION_TABLES = ("institutions", "education_entries")
CATALOGUE_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def retention_counts(files: dict[str, int], *, idempotency_keys: int) -> dict[str, int]:
    """What the daily retention job reports — the one place its keys are named.

    The file step's keys come from `sweep_counts`; this adds the key sweep's.
    In a dry run both are what *would* be removed.
    """
    return {**files, "idempotency_keys_deleted": idempotency_keys}


class UnknownRuntimeJobError(ValueError):
    """A trigger named no runtime job declared by this application."""


@dataclass(frozen=True, slots=True)
class JobResult:
    """Machine-readable outcome; no-op is a successful terminal state."""

    name: str
    job_id: str | None
    status: str
    counts: dict[str, int] = field(default_factory=dict)


class RuntimeJobs:
    """Dispatch the seven supported jobs through one reusable surface."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        institution_file: Path | None = None,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.institution_file = institution_file
        self.report = reporter or (lambda _message: None)

    async def run(
        self,
        name: str,
        *,
        job_id: str | None = None,
        dry_run: bool = False,
        message_id: str | None = None,
    ) -> JobResult:
        if name not in RUNTIME_JOB_NAMES:
            raise UnknownRuntimeJobError(name)
        return await self._run_named(name, job_id=job_id, dry_run=dry_run, message_id=message_id)

    async def _run_named(
        self,
        name: str,
        *,
        job_id: str | None,
        dry_run: bool,
        message_id: str | None,
    ) -> JobResult:
        logger.info(
            "runtime job started",
            extra={"job_name": name, "job_id": job_id, "upstash_message_id": message_id},
        )
        methods = {
            "settle-sessions": self._settle_sessions,
            "credit-reminders": self._credit_reminders,
            "monthly-credits": self._monthly_credits,
            "expire-credits": self._expire_credits,
            "sync-institutions": self._sync_institutions,
            "refresh-next-available": self._refresh_next_available,
            "sweep-intake-files": self._sweep_intake_files,
        }
        counts = await methods[name](dry_run=dry_run)
        status = "no-op" if not any(counts.values()) else "completed"
        logger.info(
            "runtime job finished",
            extra={
                "job_name": name,
                "job_id": job_id,
                "upstash_message_id": message_id,
                "job_status": status,
                "job_counts": counts,
            },
        )
        return JobResult(name=name, job_id=job_id, status=status, counts=counts)

    def _notifier(self) -> LoopsNotifier | NullNotifier:
        return notifier_for(self.settings)

    def _calendar(self) -> GoogleCalendar | NullCalendar:
        settings = self.settings
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

    def _rooms(self) -> DailyRooms | NullRooms:
        """Daily's client for the meeting records, or none when unconfigured."""
        key = self.settings.daily_api_key
        return DailyRooms(key.get_secret_value()) if key else NullRooms()

    def _calendar_health(self) -> dict[str, str] | None:
        settings = self.settings
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

    async def _settle_sessions(self, *, dry_run: bool) -> dict[str, int]:
        engine = create_database_engine(self.settings)
        try:
            async with AsyncSession(engine) as session:
                now = dt.datetime.now(dt.UTC)
                expired = await expire_requests(session, now=now, calendar=self._calendar())
                # Presence first (#382): a party no webhook reported is read
                # from Daily's records, and a session those cannot be read for
                # waits rather than settling on silence.
                check = await confirm_presence(session, now=now, rooms=self._rooms())
                settled = await settle_attendance(
                    session, now=now, unverified=check.waiting, unread=check.unread
                )
                # After attendance, so a session that ended this hour no longer
                # holds its scheduled offering open (#218).
                finalised = await finalise_scheduled_deletions(session)
                nudged = await remind_unreviewed(session, now=now)
                # Before the drain, so the reminder goes out in this same run.
                returning = await remind_returning_mentors(session, now=now)
                # **Attendance is committed before the slower checks** (Codex on
                # #393). The records reads and the calendar health checks are
                # both serial network calls under one 120s limit; if the
                # calendar phase runs the job out, the outcomes and refunds
                # decided above must not roll back with it. Each phase above is
                # idempotent, so a retry after this point repeats nothing.
                if not dry_run:
                    await session.commit()
                oauth = self._calendar_health()
                health = (
                    {"checked": 0, "healthy": 0, "disconnected": 0, "unreachable": 0}
                    if oauth is None
                    else await check_connections(
                        session,
                        now=now,
                        reader=free_busy,
                        client_id=oauth["client_id"],
                        client_secret=oauth["client_secret"],
                        key=oauth["key"],
                    )
                )
                # **Commit what was queued before anything is sent.** If the run
                # died after the provider accepted a message but before one final
                # commit, the claims and outbox rows would roll back and the
                # retried run would queue and send them again under a new
                # idempotency key. Committed first, a failed drain leaves them
                # pending for the next run instead.
                if not dry_run:
                    await session.commit()
                sent = await drain(
                    session,
                    notifier=NullNotifier() if dry_run else self._notifier(),
                    now=now,
                )
                if dry_run:
                    await session.rollback()
                else:
                    await session.commit()
                return {
                    "expired_requests": expired,
                    "settled_sessions": settled,
                    "deleted_session_types": finalised,
                    "review_nudges": nudged,
                    "return_reminders": returning,
                    "disconnected_calendars": health["disconnected"],
                    "messages": sum(sent.values()),
                }
        finally:
            await engine.dispose()

    async def _refresh_next_available(self, *, dry_run: bool) -> dict[str, int]:
        engine = create_database_engine(self.settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                # Commits per mentor itself (see its docstring), so a run that
                # times out keeps what it finished; a dry run writes nothing.
                return await refresh_next_available(
                    session,
                    max_age=dt.timedelta(minutes=self.settings.next_available_max_age_minutes),
                    window=booking_window(self.settings),
                    # No factory on a dry run: the reader then records a dead
                    # grant in this session, which the dry run rolls back.
                    reader=free_busy_reader(
                        self.settings, None if dry_run else factory, fail_open=False
                    ),
                    dry_run=dry_run,
                )
        finally:
            await engine.dispose()

    async def _sweep_intake_files(self, *, dry_run: bool) -> dict[str, int]:
        """The daily retention job: intake files, then expired idempotency keys.

        The key sweep (#353) rides here rather than on a schedule of its own,
        because both steps exist for one reason — not keeping a person's words
        past their use — and a second schedule would be one more thing to set
        up on every environment. It runs even with no storage configured.
        """
        settings = self.settings
        now = dt.datetime.now(dt.UTC)
        engine = create_database_engine(settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                keys = await sweep_expired_keys(session, now=now, dry_run=dry_run)
            with httpx.Client(timeout=CATALOGUE_TIMEOUT) as client:
                storage = intake_storage_for(settings, client)
                if storage is None:
                    # No bucket means uploads are refused, so there are no files
                    # to sweep; a no-op rather than a failure QStash would retry.
                    logger.warning("intake file sweep skipped: intake storage is not configured")
                    files = sweep_counts()
                else:
                    async with factory() as session:
                        files = await sweep_intake_files(
                            session,
                            storage,
                            now=now,
                            retention_days=settings.intake_file_retention_days,
                            unused_hours=settings.intake_file_unused_hours,
                            dry_run=dry_run,
                        )
            return retention_counts(files, idempotency_keys=keys)
        finally:
            await engine.dispose()

    async def _credit_reminders(self, *, dry_run: bool) -> dict[str, int]:
        engine = create_database_engine(self.settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                now = dt.datetime.now(dt.UTC)
                if dry_run:
                    owed = 0
                    for reminder in CREDIT_REMINDERS:
                        owed += len(await expiring_soon(session, reminder, now=now))
                    return {"reminders": owed}
                queued = await remind_about_expiring_credits(session, now=now)
                await session.commit()
                return {"reminders": queued}
        finally:
            await engine.dispose()

    async def _monthly_credits(self, *, dry_run: bool) -> dict[str, int]:
        engine = create_database_engine(self.settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                if dry_run:
                    return {"grants": await unlocked_mentee_count(session)}
                # The ladder from *these* settings rather than the process cache.
                # `RuntimeJobs` is the composition root the script used to be, so
                # the choice belongs here (settled decision #44).
                granted = await grant_monthly_credits(
                    session,
                    now=dt.datetime.now(dt.UTC),
                    ladder=credit_ladder(self.settings),
                )
                await session.commit()
                return {"grants": granted}
        finally:
            await engine.dispose()

    async def _expire_credits(self, *, dry_run: bool) -> dict[str, int]:
        engine = create_database_engine(self.settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                now = dt.datetime.now(dt.UTC)
                if dry_run:
                    return {"expired_lots": await expirable_credit_count(session, now=now)}
                expired = await expire_credits(session, now=now)
                await session.commit()
                return {"expired_lots": expired}
        finally:
            await engine.dispose()

    def _fetch_catalogue(self) -> Any:
        if self.institution_file:
            return FileCatalogue(self.institution_file).fetch()
        with httpx.Client(timeout=CATALOGUE_TIMEOUT, follow_redirects=True) as client:
            return HipolabsCatalogue(client).fetch()

    async def _sync_institutions(self, *, dry_run: bool) -> dict[str, int]:
        # The source adapter is intentionally synchronous. Moving it to a worker
        # keeps a 10k-row network fetch and JSON decode off Railway's ASGI loop.
        catalogue = await asyncio.to_thread(self._fetch_catalogue)
        rows: list[CatalogueRow] = []
        refusals: list[str] = []
        for record in catalogue.records:
            try:
                rows.append(to_catalogue_row(record))
            except CatalogueError as exc:
                refusals.append(str(exc))
        self.report(f"catalogue      {len(catalogue.records)} records")
        self.report(f"commit         {catalogue.source_commit or '(unknown)'}")
        self.report(f"usable rows    {len(rows)}")
        if refusals:
            self.report(f"refused        {len(refusals)}")
            for detail in refusals[:10]:
                self.report(f"   {detail}")
        if not rows:
            raise ValidationError("the institution catalogue has no usable rows")
        if dry_run:
            self.report("\ndry run — nothing written")
            return {"catalogue_records": len(catalogue.records), "usable_rows": len(rows)}

        engine = create_database_engine(self.settings)
        try:
            async with engine.begin() as connection:
                countries = await country_ids(connection)
                async with timestamps_from_source_across(connection, INSTITUTION_TABLES):
                    mirrored = await mirror(
                        connection, rows, countries, synced_at=dt.datetime.now(dt.UTC)
                    )
                    links = await link_education(connection)
            return {
                "catalogue_records": len(catalogue.records),
                "usable_rows": len(rows),
                "refused_rows": len(refusals),
                "mirrored": mirrored.written,
                "linked": links.linked,
                "unmatched": len(links.unmatched),
                "ambiguous": len(links.ambiguous),
            }
        finally:
            await engine.dispose()

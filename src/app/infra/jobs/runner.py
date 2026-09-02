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
from app.domain.credits import credit_ladder
from app.domain.institutions import CatalogueError, CatalogueRow, to_catalogue_row
from app.infra.clients.hipolabs import FileCatalogue, HipolabsCatalogue
from app.infra.clients.meetings import GoogleCalendar, NullCalendar, free_busy
from app.infra.clients.notifications import LoopsNotifier, NullNotifier
from app.infra.clients.templates import LoopsTemplates
from app.infra.db.calendar_store import check_connections
from app.infra.db.credit_expiry import expirable_credit_count, expire_credits
from app.infra.db.credit_grants import grant_monthly_credits, unlocked_mentee_count
from app.infra.db.credit_reminders import (
    CREDIT_REMINDERS,
    expiring_soon,
    remind_about_expiring_credits,
)
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.outbox import drain
from app.infra.db.session_writer import expire_requests, remind_unreviewed, settle_attendance
from app.infra.db.triggers import timestamps_from_source_across
from app.infra.etl.institutions import country_ids, link_education, mirror
from app.infra.jobs.manifest import RUNTIME_JOB_NAMES

logger = logging.getLogger(__name__)
INSTITUTION_TABLES = ("institutions", "education_entries")
CATALOGUE_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


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
    """Dispatch the five supported jobs through one reusable surface."""

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
        key = self.settings.loops_api_key
        if key is None:
            return NullNotifier()
        secret = key.get_secret_value()
        return (
            LoopsNotifier(secret)
            .with_settings(self.settings)
            .with_templates(LoopsTemplates(secret))
        )

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
                settled = await settle_attendance(session, now=now)
                nudged = await remind_unreviewed(session, now=now)
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
                    "review_nudges": nudged,
                    "disconnected_calendars": health["disconnected"],
                    "messages": sum(sent.values()),
                }
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

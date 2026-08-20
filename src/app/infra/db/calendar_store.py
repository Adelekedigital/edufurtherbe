"""Reading and writing a mentor's calendar grant.

**Every statement is scoped to the mentor**, on read and on write, because a
connection row holds a credential: a query that reached the wrong row would not
merely leak a fact, it would hand one person another person's calendar.

**A disconnect revokes rather than deletes**, so *that they once connected*
stays answerable — which is what a mentor asking why their calendar stopped
being consulted actually needs, and what an admin looking at a support ticket
has to be able to see.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import UtcInterval
from app.domain.notifications import Notification
from app.infra.clients.meetings import (
    CalendarAccessRevokedError,
    VenueUnavailableError,
    free_busy,
)
from app.infra.clients.secrets import SealError, unseal
from app.infra.db.models.availability import CalendarConnection
from app.infra.db.outbox import enqueue

logger = logging.getLogger(__name__)

__all__ = [
    "STALE_AFTER",
    "MentorFreeBusy",
    "NullFreeBusy",
    "active_connection",
    "check_connections",
    "connect",
    "connections_due",
    "disconnect",
    "freebusy_token",
    "record_failure",
    "record_success",
]

PROVIDER = "google"


async def connect(
    session: AsyncSession,
    user_id: UUID,
    *,
    refresh_token_encrypted: str,
) -> None:
    """Record a grant, replacing whatever this mentor had before. No commit.

    **Upsert rather than insert**, because reconnecting is the ordinary case: a
    mentor who revoked in Google's own settings, or who wants a different
    account. A plain insert would collide with the partial unique index and
    surface as a `409` on a screen where the user did nothing wrong.

    The conflict target is that partial index, so it matches only an *active*
    row — a revoked one is left exactly as it is, and the new grant is a new
    row. That is deliberate: the history of connections is a sequence, not a
    single mutable fact.
    """
    await session.execute(
        insert(CalendarConnection)
        .values(
            user_id=user_id,
            provider=PROVIDER,
            refresh_token_encrypted=refresh_token_encrypted,
            status="active",
        )
        .on_conflict_do_update(
            index_elements=["user_id", "provider"],
            index_where=CalendarConnection.status == "active",
            set_={
                "refresh_token_encrypted": refresh_token_encrypted,
                # Cleared, because a reconnection is precisely the fix for
                # whatever the last error was — leaving it would show a mentor a
                # complaint about a connection that now works.
                "last_error": None,
                "status": "active",
            },
        )
    )


async def active_connection(session: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
    """This mentor's live grant, or ``None``.

    **No token in the result.** Every caller so far wants to know *whether*
    the mentor is connected and what to show them about it; the free/busy read
    that needs the credential will ask for it by name, and a general accessor
    handing one out is how it ends up somewhere it should not be.

    **Includes a grant that broke**, which it did not until the health check
    existed to find one. Before that, a revoked grant read as *never connected*
    and the mentor had nothing to act on — the gap ADR 0004 names in its own
    Confirmation section. `status` is returned so a client can tell the two
    apart rather than inferring it from `last_error` being set.
    """
    row = (
        (
            await session.execute(
                select(
                    CalendarConnection.id,
                    CalendarConnection.status,
                    CalendarConnection.connected_at,
                    CalendarConnection.last_synced_at,
                    CalendarConnection.last_error,
                ).where(
                    CalendarConnection.user_id == user_id,
                    CalendarConnection.provider == PROVIDER,
                    # **`error` too, and `revoked` deliberately not.** A grant
                    # that broke is something the mentor needs to see and fix;
                    # one they disconnected on purpose is something they already
                    # know, and showing it back to them would read as a
                    # disconnect that did not work.
                    CalendarConnection.status.in_(("active", "error")),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def disconnect(session: AsyncSession, user_id: UUID) -> bool:
    """Revoke this mentor's grant. Returns whether there was one. No commit.

    **The token is cleared, not just the status.** A revoked row is a record
    that a connection existed, and it needs none of the credential to say so —
    keeping one would mean a disconnect that left the platform able to read the
    calendar it had just been told to stop reading.

    ``''`` rather than null because the column is ``NOT NULL``, and relaxing
    that to express *revoked* would make a token-less **active** row
    representable. The status says which state this is; the column only has to
    hold nothing useful.
    """
    result = await session.execute(
        update(CalendarConnection)
        .where(
            CalendarConnection.user_id == user_id,
            CalendarConnection.provider == PROVIDER,
            CalendarConnection.status == "active",
        )
        .values(status="revoked", refresh_token_encrypted="", last_error=None)
        .returning(CalendarConnection.id)
    )
    return result.scalar_one_or_none() is not None


async def freebusy_token(session: AsyncSession, user_id: UUID) -> str | None:
    """This mentor's sealed refresh token, or ``None`` if there is no live grant.

    **Its own accessor rather than a field on `active_connection`.** That one is
    read to render a screen, and a general accessor handing out a credential is
    how one ends up somewhere it should not be. A caller that wants the token
    has to say so, and this is the only place that says yes.
    """
    return (
        await session.execute(
            select(CalendarConnection.refresh_token_encrypted).where(
                CalendarConnection.user_id == user_id,
                CalendarConnection.provider == PROVIDER,
                CalendarConnection.status == "active",
            )
        )
    ).scalar_one_or_none()


async def record_failure(session: AsyncSession, user_id: UUID, error: str) -> bool:
    """Mark a grant dead. **Only for a failure that retrying cannot fix.**

    Deferred out of the PR that built this table precisely because the
    transient-versus-fatal line could not be drawn honestly without knowing what
    Google actually returns. It can now: `invalid_grant` on the token exchange,
    and a per-calendar ``errors`` array in a `freeBusy` body, both mean the
    grant is gone. A timeout or a 503 means nothing of the sort and must not
    reach here — it would turn every blip into a re-consent.

    **The token is cleared**, because a grant Google has rejected is no longer a
    credential; keeping it would leave a dead secret at rest for no purpose.
    ``''`` rather than null for the reason :func:`disconnect` gives.

    Self-limiting, which is what makes it safe on a read path: once the status
    leaves ``active`` this mentor is invisible to :func:`freebusy_token`, so no
    further call is made and no further write happens.

    **Telling the mentor happens here rather than in the callers**, of which
    there are two — the free/busy read that trips over a dead grant, and the
    health sweep that goes looking for one. A message owed from one path and not
    the other is the defect this shape forecloses, and it costs nothing to make
    unreachable.

    Returns whether a row moved. ``False`` means the grant was already dead, and
    is what keeps a mentor from being told again on every sweep: the ``WHERE``
    only matches ``active``, so the second call updates nothing and enqueues
    nothing.
    """
    row = (
        await session.execute(
            update(CalendarConnection)
            .where(
                CalendarConnection.user_id == user_id,
                CalendarConnection.provider == PROVIDER,
                CalendarConnection.status == "active",
            )
            .values(status="error", refresh_token_encrypted="", last_error=error[:500])
            .returning(CalendarConnection.id)
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    await enqueue(
        session,
        Notification.CALENDAR_DISCONNECTED,
        entity_type="calendar_connection",
        entity_id=row,
        recipient_ids=(user_id,),
        variables={"reason": error[:500]},
    )
    return True


async def record_success(session: AsyncSession, user_id: UUID, *, when: dt.datetime) -> None:
    """Stamp a grant as confirmed working. No commit.

    **Written by the sweep and never by the read path.** A slot render never
    commits, so a stamp made there would be rolled back with the rest of the
    request — and giving it its own transaction, the way a dead grant gets one,
    would mean a write on every public slot render for every connected mentor.
    The sweep already owns a transaction and already runs on a schedule.

    So ``last_synced_at`` answers *when this connection was last confirmed to
    work*, which is what a mentor wants to know and what lets the sweep skip
    everything it checked recently.
    """
    await session.execute(
        update(CalendarConnection)
        .where(
            CalendarConnection.user_id == user_id,
            CalendarConnection.provider == PROVIDER,
            CalendarConnection.status == "active",
        )
        .values(last_synced_at=when, last_error=None)
    )


async def connections_due(
    session: AsyncSession, *, now: dt.datetime, stale_after: dt.timedelta
) -> tuple[UUID, ...]:
    """Mentors whose calendar has not been confirmed working recently.

    **Never-checked first, by ordering nulls first**, because a grant that has
    never been probed is the one most likely to be broken — a mentor who
    connected and then revoked in Google's settings looks exactly like this.

    The staleness filter is what keeps this from being 44 Google calls an hour
    forever. It is a filter rather than a limit: a bound on *count* would leave
    the same connections at the back of the queue every run.
    """
    return tuple(
        (
            await session.execute(
                select(CalendarConnection.user_id)
                .where(
                    CalendarConnection.provider == PROVIDER,
                    CalendarConnection.status == "active",
                    or_(
                        CalendarConnection.last_synced_at.is_(None),
                        CalendarConnection.last_synced_at < now - stale_after,
                    ),
                )
                .order_by(CalendarConnection.last_synced_at.asc().nullsfirst())
            )
        )
        .scalars()
        .all()
    )


#: How long a confirmed-working connection is trusted before it is probed again.
#:
#: **Twelve hours, and the cost of being wrong is small in one direction only.**
#: Too long means a mentor learns late that their calendar stopped being read;
#: too short means an outbound request per mentor per run for no new
#: information. A revoked grant already surfaces reactively the moment anybody
#: renders that mentor's slots, so this is the backstop for the mentor nobody
#: happens to look at — which is exactly the case ADR 0004 says went unnoticed.
STALE_AFTER = dt.timedelta(hours=12)


async def check_connections(
    session: AsyncSession,
    *,
    now: dt.datetime,
    reader: Callable[..., tuple[UtcInterval, ...]],
    client_id: str,
    client_secret: str,
    key: str,
    stale_after: dt.timedelta = STALE_AFTER,
) -> dict[str, int]:
    """Probe every calendar grant nobody has confirmed lately. Does not commit.

    **The gap ADR 0004 names in its own Confirmation section**: *"nothing
    currently alerts when connected accounts are revoked. That is precisely how
    this situation went unnoticed."* A grant revoked in Google's settings sends
    us nothing, so the only way to find out is to ask.

    Reactive detection already exists — a free/busy read that trips over a dead
    grant records it — but it needs somebody to render that mentor's slots. A
    mentor nobody browses is a mentor whose calendar quietly stops being
    consulted, which is the failure this closes.

    **A transient failure is left completely alone**: not marked, not stamped,
    not counted as checked. So the next run tries it again, which is what
    distinguishes *Google was busy* from *this grant is dead* without having to
    decide on one sample.

    Returns counts rather than raising, because one unreachable mentor must not
    stop the sweep reaching the rest.
    """
    counts = {"checked": 0, "healthy": 0, "disconnected": 0, "unreachable": 0}
    for user_id in await connections_due(session, now=now, stale_after=stale_after):
        sealed = await freebusy_token(session, user_id)
        if not sealed:  # pragma: no cover - the row was just selected as active
            continue
        counts["checked"] += 1
        try:
            token = unseal(sealed, key=key)
        except SealError:
            # The key rotated, so this token can never be opened again. As dead
            # as a revoked grant, and the mentor's fix is the same: reconnect.
            await record_failure(session, user_id, "the stored credential could not be opened")
            counts["disconnected"] += 1
            continue

        try:
            # **A one-hour window, because the answer is not what is wanted.**
            # This asks whether the grant still works, and the narrowest
            # question Google will answer is the cheapest one to ask.
            reader(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=token,
                start=now,
                end=now + dt.timedelta(hours=1),
            )
        except CalendarAccessRevokedError as exc:
            await record_failure(session, user_id, str(exc))
            counts["disconnected"] += 1
        except VenueUnavailableError as exc:
            logger.info("calendar for %s could not be checked: %s", user_id, exc)
            counts["unreachable"] += 1
        else:
            await record_success(session, user_id, when=now)
            counts["healthy"] += 1
    return counts


class NullFreeBusy:
    """Subtracts nothing. The default everywhere.

    **Not a stub for something missing.** Most mentors have connected no
    calendar and never will, and for them this *is* the correct answer — so the
    default keeps every existing slot query byte-identical rather than making
    the unconfigured path a special case.
    """

    async def busy(
        self,
        session: AsyncSession,
        user_id: UUID,
        start: dt.datetime,
        end: dt.datetime,
    ) -> tuple[UtcInterval, ...]:
        del session, user_id, start, end
        return ()


class MentorFreeBusy:
    """A mentor's external commitments, read from Google at the moment of asking.

    **Never polled and never mirrored** (ADR 0004). What comes back is intervals
    and nothing else — the scope grants times, not contents — so this cannot
    learn what a mentor is doing even in principle, which is what made the ask
    defensible to them.

    **It fails open, and that is ADR 0004's own reasoning rather than
    convenience.** That record calls free/busy *advisory* and says it "must
    never be treated as the mechanism that prevents double booking" — the
    exclusion constraint is. An advisory check that refuses a booking when
    Google is slow has been promoted to authoritative, and it would make a
    third party's outage an outage here. So a failed read subtracts nothing and
    the caller proceeds on declared availability alone, which is exactly what
    every mentor without a connection already gets.

    The cost of failing open is stated rather than hidden: for the length of an
    outage a mentee can book over a real conflict, and the mentor resolves it by
    cancelling. The cost of failing closed is that nobody can book at all.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        key: str,
        reader: Callable[..., tuple[UtcInterval, ...]] = free_busy,
        session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._key = key
        self._reader = reader
        self._session_factory = session_factory

    async def _mark_dead(self, session: AsyncSession, user_id: UUID, reason: str) -> None:
        """Record a dead grant **outside the caller's transaction**.

        Not a refinement — without it the write does not survive. A slot render
        never commits, and a booking that later raises rolls the whole unit back,
        so a failure recorded on the caller's session is a failure nobody ever
        sees. The mentor's connection would stay `active` and every public slot
        request for them would keep paying a Google round trip that cannot
        succeed, forever. Caught by a test asserting the second request makes no
        call, which it did.

        Equally it must **not** commit the caller's work: `book_session` shares
        this session with a half-written booking, and committing there would
        publish it.

        Falls back to the caller's session when no factory was wired, which is
        the unit-test path — the write is then only as durable as that
        transaction, and no caller in production is in that position.
        """
        if self._session_factory is None:
            await record_failure(session, user_id, reason)
            return
        async with self._session_factory() as own:
            await record_failure(own, user_id, reason)
            await own.commit()

    async def busy(
        self,
        session: AsyncSession,
        user_id: UUID,
        start: dt.datetime,
        end: dt.datetime,
    ) -> tuple[UtcInterval, ...]:
        """What Google says, or ``()`` and a reason recorded somewhere.

        **No connection means no call.** Checked first because it is the common
        case — 44 mentors, most of them unconnected — and because the
        alternative is an outbound request per slot render on an endpoint that
        takes no token.
        """
        sealed = await freebusy_token(session, user_id)
        if not sealed:
            return ()

        try:
            token = unseal(sealed, key=self._key)
        except SealError:
            # The key rotated, so this token can never be opened again. That is
            # as dead as a revoked grant and is recorded the same way — the
            # mentor reconnects, which is the only thing that can fix it.
            logger.warning("calendar token for %s cannot be opened; marking it dead", user_id)
            await self._mark_dead(session, user_id, "the stored credential could not be opened")
            return ()

        try:
            return self._reader(
                client_id=self._client_id,
                client_secret=self._client_secret,
                refresh_token=token,
                start=start,
                end=end,
            )
        except CalendarAccessRevokedError as exc:
            # **The one failure worth a write.** The mentor revoked us, so every
            # future call fails identically; recording it stops the calls and is
            # the only way they find out — ADR 0004 names the absence of exactly
            # this as a live gap.
            logger.info("calendar grant for %s is dead: %s", user_id, exc)
            await self._mark_dead(session, user_id, str(exc))
            return ()
        except VenueUnavailableError as exc:
            # Transient. Logged and nothing else: writing here would turn a
            # rate limit into a re-consent, and would put a write on a public
            # read path that an anonymous caller could drive.
            logger.info("free/busy unavailable for %s: %s", user_id, exc)
            return ()

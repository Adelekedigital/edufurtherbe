"""Finding a calendar grant that died without anybody looking.

**ADR 0004 names this gap in its own Confirmation section** — *"nothing
currently alerts when connected accounts are revoked. That is precisely how this
situation went unnoticed."* A grant revoked in Google's settings sends us
nothing, so the only way to find out is to ask.

Reactive detection already existed: a free/busy read that trips over a dead
grant records it. But that needs somebody to render that mentor's slots, so a
mentor nobody browses is a mentor whose calendar quietly stops being consulted.
That is what these cover.

**The negative cases carry the weight.** A sweep that marks connections dead is
easy; one that leaves a rate-limited mentor alone, and tells each mentor exactly
once no matter how often it runs, is the part that goes wrong.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import make_public_mentor

from app.domain.availability import UtcInterval
from app.infra.clients.meetings import CalendarAccessRevokedError, VenueUnavailableError
from app.infra.clients.secrets import seal
from app.infra.db.calendar_store import (
    check_connections,
    connections_due,
    record_failure,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

KEY = Fernet.generate_key().decode()
NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


class FakeGoogle:
    """Answers the probe, and records how many it was asked."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[UtcInterval, ...]:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return ()


async def sweep(engine: AsyncEngine, google: FakeGoogle, **kwargs: Any) -> dict[str, int]:
    """One sweep in its own transaction, committed — as the job runs it."""
    async with AsyncSession(engine) as session:
        counts = await check_connections(
            session,
            now=kwargs.pop("now", NOW),
            reader=google,
            client_id="cid",
            client_secret="secret",  # noqa: S106
            key=KEY,
            **kwargs,
        )
        await session.commit()
    return counts


async def a_connected_mentor(
    engine: AsyncEngine,
    tag: str,
    *,
    last_synced_at: dt.datetime | None = None,
    token: str = "a-refresh-token",  # noqa: S107
) -> UUID:
    """`token` is how a test tells one mentor's probe from another's."""
    mentor = await make_public_mentor(engine, tag)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO calendar_connections "
                "(user_id, provider, refresh_token_encrypted, status, last_synced_at) "
                "VALUES (:u, 'google', :t, 'active', :s)"
            ),
            {"u": mentor, "t": seal(token, key=KEY), "s": last_synced_at},
        )
    return mentor


async def connection(engine: AsyncEngine, mentor: UUID) -> dict[str, Any]:
    async with engine.begin() as conn:
        return dict(
            (
                await conn.execute(
                    text(
                        "SELECT status, last_error, last_synced_at, refresh_token_encrypted "
                        "FROM calendar_connections WHERE user_id = :u"
                    ),
                    {"u": mentor},
                )
            )
            .mappings()
            .one()
        )


async def queued(engine: AsyncEngine, mentor: UUID) -> list[str]:
    async with engine.begin() as conn:
        return [
            str(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT event_type FROM outbox_events WHERE payload ->> 'recipient_id' = :u"
                    ),
                    {"u": str(mentor)},
                )
            ).scalars()
        ]


# --------------------------------------------------------------------------
# Finding a dead grant
# --------------------------------------------------------------------------


async def test_a_revoked_grant_is_found_without_anybody_browsing(
    db_engine: AsyncEngine,
) -> None:
    """The whole point: no page view required."""
    mentor = await a_connected_mentor(db_engine, "health-dead")
    google = FakeGoogle(raises=CalendarAccessRevokedError("the grant was revoked"))

    counts = await sweep(db_engine, google)

    assert counts["disconnected"] == 1
    row = await connection(db_engine, mentor)
    assert row["status"] == "error"
    assert "revoked" in row["last_error"]
    # A grant Google has rejected is no longer a credential.
    assert row["refresh_token_encrypted"] == ""


async def test_the_mentor_is_told(
    db_engine: AsyncEngine,
) -> None:
    """Detecting it and saying nothing would leave the gap exactly where it was."""
    mentor = await a_connected_mentor(db_engine, "health-told")
    await sweep(db_engine, FakeGoogle(raises=CalendarAccessRevokedError("revoked")))

    assert await queued(db_engine, mentor) == ["calendar_disconnected"]


async def test_a_second_sweep_does_not_tell_them_again(
    db_engine: AsyncEngine,
) -> None:
    """**The bug this shape forecloses.** The sweep runs hourly.

    `record_failure` only matches an `active` row, so the second attempt writes
    nothing and enqueues nothing — and the connection is no longer `active`, so
    it is not even selected. Both guards are asserted because either alone would
    look like it worked.
    """
    mentor = await a_connected_mentor(db_engine, "health-once")
    google = FakeGoogle(raises=CalendarAccessRevokedError("revoked"))
    await sweep(db_engine, google)

    second = await sweep(db_engine, google)

    assert second["checked"] == 0
    assert len(google.calls) == 1
    assert await queued(db_engine, mentor) == ["calendar_disconnected"]


async def test_recording_the_same_death_twice_tells_them_once(
    db_engine: AsyncEngine,
) -> None:
    """**`record_failure`'s own guard, asserted at its own level.**

    The sweep never calls it twice — a dead connection is no longer `active`, so
    it is not selected again — which means the test above passes with the guard
    removed. It proves the *selection*, not the guard.

    The guard still matters, for the path the sweep is not: two slot renders for
    the same mentor can both read a live token, both hear `invalid_grant`, and
    both record it. The first moves the row; the second matches nothing and must
    stay silent rather than sending a duplicate.
    """
    mentor = await a_connected_mentor(db_engine, "health-twice")

    async with AsyncSession(db_engine) as session:
        first = await record_failure(session, mentor, "revoked")
        second = await record_failure(session, mentor, "revoked again")
        await session.commit()

    assert (first, second) == (True, False)
    assert await queued(db_engine, mentor) == ["calendar_disconnected"]
    # The first message wins: the second call found nothing to change, so it did
    # not overwrite the reason either.
    assert (await connection(db_engine, mentor))["last_error"] == "revoked"


# --------------------------------------------------------------------------
# Leaving a working one alone
# --------------------------------------------------------------------------


async def test_a_healthy_grant_is_stamped_and_then_skipped(
    db_engine: AsyncEngine,
) -> None:
    """`last_synced_at` had no writer at all before this — the API described it
    as "when the platform last read your busy hours" and it was null forever."""
    mentor = await a_connected_mentor(db_engine, "health-ok")
    google = FakeGoogle()

    first = await sweep(db_engine, google)
    second = await sweep(db_engine, google)

    assert first["healthy"] == 1
    assert (await connection(db_engine, mentor))["last_synced_at"] == NOW
    assert second["checked"] == 0, "a connection confirmed minutes ago is not re-probed"
    assert len(google.calls) == 1


async def test_a_stale_grant_is_probed_again(
    db_engine: AsyncEngine,
) -> None:
    """Otherwise one success would trust a connection forever."""
    await a_connected_mentor(db_engine, "health-stale", last_synced_at=NOW - dt.timedelta(days=2))
    google = FakeGoogle()

    counts = await sweep(db_engine, google)

    assert counts["checked"] == 1
    assert counts["healthy"] == 1


async def test_a_never_checked_grant_is_probed_before_a_merely_stale_one(
    db_engine: AsyncEngine,
) -> None:
    """Nulls first, because a grant nobody has ever confirmed is the likeliest
    to be broken — a mentor who connected and then revoked looks exactly so.

    Both are due here; the assertion is the *order*, which an earlier version of
    this test claimed to check and did not.
    """
    await a_connected_mentor(
        db_engine,
        "health-order-stale",
        last_synced_at=NOW - dt.timedelta(days=2),
        token="stale",  # noqa: S106
    )
    await a_connected_mentor(db_engine, "health-order-never", token="never")  # noqa: S106
    google = FakeGoogle()

    await sweep(db_engine, google)

    assert [call["refresh_token"] for call in google.calls] == ["never", "stale"]


async def test_a_connection_confirmed_moments_ago_is_not_due(
    db_engine: AsyncEngine,
) -> None:
    """The staleness filter is what stops this being a request per mentor per
    run forever. Asserted at the boundary rather than only well past it."""
    await a_connected_mentor(db_engine, "health-boundary", last_synced_at=NOW)
    google = FakeGoogle()

    counts = await sweep(db_engine, google, stale_after=dt.timedelta(hours=12))

    assert counts["checked"] == 0
    assert google.calls == []


# --------------------------------------------------------------------------
# A bad afternoon is not a dead grant
# --------------------------------------------------------------------------


async def test_an_unreachable_google_changes_nothing(
    db_engine: AsyncEngine,
) -> None:
    """**The failure that must not cost a mentor their connection.**

    Not marked, not stamped, not told. Leaving `last_synced_at` alone is what
    makes the next run try again — stamping it on a failed probe would treat an
    outage as twelve hours of confirmed health.
    """
    mentor = await a_connected_mentor(db_engine, "health-blip")

    counts = await sweep(db_engine, FakeGoogle(raises=VenueUnavailableError("429")))

    assert counts == {"checked": 1, "healthy": 0, "disconnected": 0, "unreachable": 1}
    row = await connection(db_engine, mentor)
    assert row["status"] == "active"
    assert row["last_synced_at"] is None
    assert await queued(db_engine, mentor) == []


async def test_one_unreachable_mentor_does_not_stop_the_sweep(
    db_engine: AsyncEngine,
) -> None:
    """A sweep that raised on the first bad grant would leave the rest unchecked
    forever, because the same one would be first again next run."""

    class Alternating(FakeGoogle):
        def __call__(self, **kwargs: Any) -> tuple[UtcInterval, ...]:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise VenueUnavailableError("first one is down")
            return ()

    await a_connected_mentor(db_engine, "health-many-a")
    await a_connected_mentor(db_engine, "health-many-b")

    counts = await sweep(db_engine, Alternating())

    assert counts["checked"] == 2
    assert counts["unreachable"] == 1
    assert counts["healthy"] == 1


async def test_a_credential_that_cannot_be_opened_is_treated_as_dead(
    db_engine: AsyncEngine,
) -> None:
    """A rotated key. Retrying cannot fix it, and the mentor's fix is the same
    as for a revoked grant — so it is recorded the same way."""
    mentor = await make_public_mentor(db_engine, "health-badkey")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO calendar_connections "
                "(user_id, provider, refresh_token_encrypted, status) "
                "VALUES (:u, 'google', :t, 'active')"
            ),
            {"u": mentor, "t": seal("token", key=Fernet.generate_key().decode())},
        )
    google = FakeGoogle()

    counts = await sweep(db_engine, google)

    assert counts["disconnected"] == 1
    assert google.calls == [], "a token we cannot open is never sent to Google"
    assert (await connection(db_engine, mentor))["status"] == "error"


# --------------------------------------------------------------------------
# What is not touched
# --------------------------------------------------------------------------


async def test_a_disconnected_mentor_is_not_probed(
    db_engine: AsyncEngine,
) -> None:
    """They asked us to stop. Probing anyway would be a request per sweep,
    forever, against a grant we were told to forget."""
    mentor = await a_connected_mentor(db_engine, "health-revoked")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE calendar_connections SET status = 'revoked', "
                "refresh_token_encrypted = '' WHERE user_id = :u"
            ),
            {"u": mentor},
        )
    google = FakeGoogle()

    counts = await sweep(db_engine, google)

    assert counts["checked"] == 0
    assert google.calls == []


async def test_the_due_query_itself_returns_only_live_grants(
    db_engine: AsyncEngine,
) -> None:
    """**The filter at its own level.** The sweep looks up a token before
    calling anything, so a revoked row selected here would be dropped a step
    later and the test above would still pass — it proves the token lookup, not
    the query.

    The filter is what keeps this from being a pointless lookup per dead
    connection on every run, forever.
    """
    live = await a_connected_mentor(db_engine, "health-due-live")
    for tag, status in (("health-due-revoked", "revoked"), ("health-due-error", "error")):
        gone = await a_connected_mentor(db_engine, tag)
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE calendar_connections SET status = :s WHERE user_id = :u"),
                {"s": status, "u": gone},
            )

    async with AsyncSession(db_engine) as session:
        due = await connections_due(session, now=NOW, stale_after=dt.timedelta(hours=12))

    assert due == (live,)


async def test_the_probe_asks_for_the_narrowest_window(
    db_engine: AsyncEngine,
) -> None:
    """The answer is not wanted — only whether the grant still works."""
    await a_connected_mentor(db_engine, "health-window")
    google = FakeGoogle()

    await sweep(db_engine, google)

    (call,) = google.calls
    assert call["start"] == NOW
    assert call["end"] - call["start"] == dt.timedelta(hours=1)


async def test_a_mentor_with_no_connection_is_never_reached(
    db_engine: AsyncEngine,
) -> None:
    """Most mentors, and they must cost nothing."""
    await make_public_mentor(db_engine, "health-none")
    google = FakeGoogle()

    counts = await sweep(db_engine, google)

    assert counts["checked"] == 0
    assert google.calls == []

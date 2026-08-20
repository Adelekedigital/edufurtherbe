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

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.availability import CalendarConnection

__all__ = ["active_connection", "connect", "disconnect"]

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
    """
    row = (
        (
            await session.execute(
                select(
                    CalendarConnection.id,
                    CalendarConnection.connected_at,
                    CalendarConnection.last_synced_at,
                    CalendarConnection.last_error,
                ).where(
                    CalendarConnection.user_id == user_id,
                    CalendarConnection.provider == PROVIDER,
                    CalendarConnection.status == "active",
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

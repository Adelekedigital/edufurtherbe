"""Reserve a key, or hand back the answer it already has.

**The table is a replay cache in front of a constraint, not the control.** What
actually makes a double booking impossible is
``sessions_no_mentor_double_booking``: two bookings for one mentor at one
instant overlap by definition, so the second is refused by the database whether
or not a key was sent. This module's job is narrower and worth stating plainly —
it turns that refusal into the *original answer* for the honest client whose
connection dropped and who is retrying, which is a sequential story rather than
a concurrent one.

Saying so matters, because the opposite belief is what makes an idempotency
table dangerous: a reader who thinks this is the control will eventually relax
the constraint behind it.

**Four outcomes, returned as four types rather than signalled.** They are not
exceptional — three of them are ordinary answers to an ordinary request — and a
union makes every one of them visible at the call site, where a bare ``None``
would collapse "in flight" into "mismatched" and be handled by whichever branch
was written first.

===============================  ==========================================
row absent, or expired           :class:`Held` — do the work
row present, hash differs        :class:`Mismatched` — 422
row present, no ``completed_at`` :class:`InFlight` — 409
row present, completed           :class:`Replayed` — the stored answer
===============================  ==========================================

**Expiry is enforced here, not by a job.** A row past ``expires_at`` is
invisible to the lookup and is reclaimed in place by the next reservation for
the same key, so the table self-heals. The retention sweep the runbook lists
still earns its place — it keeps the table small — but nothing depends on it
having run, which is the difference between a cache and a leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.platform import TTL_SQL, IdempotencyKey

__all__ = [
    "Held",
    "InFlight",
    "Mismatched",
    "Replayed",
    "Reservation",
    "record_response",
    "reserve",
]

#: The column default, reused. The reclaim path below sets `expires_at`
#: explicitly, and two windows that disagreed would be a defect nobody could
#: see — the row would simply live for whichever wrote last.
TTL = text(TTL_SQL)


@dataclass(frozen=True, slots=True)
class Held:
    """The key is ours. Do the work, then :func:`record_response` against this id."""

    id: UUID


@dataclass(frozen=True, slots=True)
class Replayed:
    """The key already has an answer. Return it verbatim.

    The status code is carried rather than assumed: this endpoint has one
    success code today, and a replay that invented ``200`` for a stored ``201``
    would be a different answer to the same request.
    """

    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InFlight:
    """A request with this key is still running, or died without finishing."""


@dataclass(frozen=True, slots=True)
class Mismatched:
    """This key already stands for a *different* request."""


Reservation = Held | Replayed | InFlight | Mismatched


async def reserve(
    session: AsyncSession,
    *,
    key: str,
    user_id: UUID,
    endpoint: str,
    request_hash: str,
) -> Reservation:
    """Claim the key, or say what already holds it.

    **One statement claims or reclaims**, and the ``WHERE`` on the conflict
    branch is what makes it safe: an unexpired row is left exactly as it is and
    nothing is returned, while an expired one is reset in place. Doing it as a
    select-then-insert would leave a window where two requests both see nothing.

    **``DO UPDATE`` rather than ``DO NOTHING``, deliberately.** They differ on
    the case that matters: against a *concurrent, uncommitted* conflicting row,
    ``DO UPDATE`` waits for that transaction to finish, while ``DO NOTHING`` may
    return immediately having seen nothing. Waiting is what lets the second
    request read the first's committed answer and replay it, instead of being
    told the key is free and booking again.

    The hash mismatch is checked **after** the reservation attempt rather than
    before, because reading first would be the select-then-insert this avoids.
    A mismatching key always conflicts, so the read below always happens for it.
    """
    claimed = (
        await session.execute(
            insert(IdempotencyKey)
            .values(
                key=key,
                user_id=user_id,
                endpoint=endpoint,
                request_hash=request_hash,
                locked_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "key"],
                set_={
                    "endpoint": endpoint,
                    "request_hash": request_hash,
                    "locked_at": func.now(),
                    "response_body": None,
                    "status_code": None,
                    "completed_at": None,
                    "expires_at": func.now() + TTL,
                },
                where=IdempotencyKey.expires_at <= func.now(),
            )
            .returning(IdempotencyKey.id)
        )
    ).scalar_one_or_none()
    if claimed is not None:
        return Held(claimed)

    # Nothing was claimed, so a live row holds this key. It is certainly visible
    # now: `DO UPDATE` waited for any concurrent writer to commit.
    #
    # **Scoped on `user_id`, and that is the authorization decision in this
    # module.** The row holds a stored response body, so a lookup on `key` alone
    # would serve one caller another caller's booking. It is also why the unique
    # index is `(user_id, key)`: with a global key space this select would find
    # nothing while the insert above collided, which is a failure with no
    # correct answer to give.
    row = (
        (
            await session.execute(
                select(
                    IdempotencyKey.request_hash,
                    IdempotencyKey.status_code,
                    IdempotencyKey.response_body,
                    IdempotencyKey.completed_at,
                ).where(
                    IdempotencyKey.user_id == user_id,
                    IdempotencyKey.key == key,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        # The holder rolled back between the conflict and this read, which is
        # exactly what a refused booking does. Nothing is reserved and nothing
        # is stored, so "a request is in flight" is the honest answer — the
        # client retries and the next attempt claims the key cleanly.
        return InFlight()
    if row["request_hash"] != request_hash:
        return Mismatched()
    if row["completed_at"] is None or row["response_body"] is None:
        return InFlight()
    return Replayed(int(row["status_code"] or 200), dict(row["response_body"]))


async def record_response(
    session: AsyncSession,
    reservation: Held,
    *,
    status_code: int,
    body: dict[str, Any],
) -> None:
    """Store the answer against the reservation. Committed by the caller.

    **In the caller's transaction, deliberately.** The answer and the thing it
    describes are written together or not at all: committing the key first would
    let a crash leave a stored ``201`` for a session that never existed, and a
    replay would then hand the client the id of nothing.
    """
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.id == reservation.id)
        .values(status_code=status_code, response_body=body, completed_at=func.now())
    )

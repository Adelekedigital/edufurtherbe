"""A finished session asks the mentee how it went, and nudges once.

**Fired by the transition, never by a clock.** `settle_attendance` decides
`completed` against `no_show` from attendance, so the request is produced by the
same statement that decides the session happened. A timer set to "end plus ten
minutes" races that sweep and can ask about a session nobody attended — decision
10, and the tests here are what make it structural rather than merely intended.

**Nothing is ever cancelled.** The reminder is scheduled ahead and re-reads
before it queues anything, which is what the callbacks module means by
*"scheduling ahead safe without ever cancelling anything"* (ADR 0025). A review
written in the meantime makes the nudge a no-op; cancelling would make every
write path responsible for unscheduling, and the bug is the nudge that fires for
a review written through the path somebody forgot.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.factories import add_session_type, make_public_mentor

from app.domain.attendance import JOIN_CLOSES
from app.domain.notifications import (
    REVIEW_REMINDER_AFTER,
    REVIEW_REMINDER_INTERVAL,
    REVIEW_REMINDER_KIND,
    Notification,
)
from app.infra.db.session_writer import remind_unreviewed, settle_attendance

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class World:
    def __init__(self, engine: AsyncEngine, mentor: UUID, mentee: UUID, offering: UUID) -> None:
        self.engine = engine
        self.mentor = mentor
        self.mentee = mentee
        self.offering = offering

    async def confirmed(self, *, offering: UUID | None = None, hours_ago: int = 3) -> UUID:
        """A confirmed session whose join window has already shut.

        Written straight to the table rather than booked: slots are always in the
        future, so a session the sweep would settle cannot be produced through
        the API at all. `test_api_attendance` says the same thing about the same
        problem.
        """
        async with self.engine.begin() as conn:
            session_id = (
                await conn.execute(
                    text(
                        "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
                        "duration_minutes, status) "
                        "VALUES (:m, :e, :t, now() - make_interval(hours => :h), 45, 'confirmed') "
                        "RETURNING id"
                    ),
                    {
                        "m": self.mentor,
                        "e": self.mentee,
                        "t": self.offering if offering is None else offering,
                        "h": hours_ago,
                    },
                )
            ).scalar_one()
            for user, role in ((self.mentor, "mentor"), (self.mentee, "mentee")):
                await conn.execute(
                    text(
                        "INSERT INTO session_participants (session_id, user_id, role, "
                        "attendance_status) VALUES (:s, :u, :r, 'attended')"
                    ),
                    {"s": session_id, "u": user, "r": role},
                )
        return UUID(str(session_id))

    async def nobody_came(self, session_id: UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE session_participants SET attendance_status = 'no_show' "
                    "WHERE session_id = :s"
                ),
                {"s": session_id},
            )

    async def settle(self, **kwargs: Any) -> int:
        factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with factory() as db:
            settled = await settle_attendance(
                db, now=dt.datetime.now(dt.UTC) + JOIN_CLOSES, **kwargs
            )
            await db.commit()
        return settled

    async def nudge(self) -> int:
        """Run the reminder sweep a day and a minute after the request.

        The clock is moved rather than the rows: the sweep asks whether the
        request is `REVIEW_REMINDER_AFTER` old, and moving `now` is the honest
        way to reach that without waiting.
        """
        factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with factory() as db:
            nudged = await remind_unreviewed(
                db, now=dt.datetime.now(dt.UTC) + REVIEW_REMINDER_AFTER + dt.timedelta(minutes=1)
            )
            await db.commit()
        return nudged

    async def queued(self, notification: Notification) -> list[dict[str, Any]]:
        async with self.engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT entity_id, payload FROM outbox_events "
                    "WHERE event_type = :t ORDER BY created_at"
                ),
                {"t": str(notification)},
            )
            return [dict(row) for row in rows.mappings()]

    async def review(self, session_id: UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO reviews (session_id, reviewed_by, reviewed_for, "
                    "communication_rating, knowledge_rating, practicality_rating, "
                    "support_rating, valuable_rating, nps_recommend_score, public_review) "
                    "SELECT :s, mentee_id, mentor_id, 3, 3, 3, 3, 5, 9, 'Done.' "
                    "FROM sessions WHERE id = :s"
                ),
                {"s": session_id},
            )


@pytest_asyncio.fixture
async def world(db_engine: AsyncEngine) -> World:
    tag = uuid4().hex[:8]
    mentor = await make_public_mentor(db_engine, tag)
    offering = await add_session_type(db_engine, mentor, name=f"CV review {tag}")
    async with db_engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Mo', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.test"},
            )
        ).scalar_one()
    return World(db_engine, mentor, UUID(str(mentee)), offering)


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


async def test_a_completed_session_asks_the_mentee(world: World) -> None:
    session_id = await world.confirmed()

    await world.settle()

    queued = await world.queued(Notification.REVIEW_REQUESTED)
    assert [row["entity_id"] for row in queued] == [session_id]
    assert queued[0]["payload"]["recipient_id"] == str(world.mentee)


async def test_only_the_mentee_is_asked(world: World) -> None:
    """**The whole reason the message this replaces was withdrawn.**

    A mentor asked to review the session they gave is one person too many, and
    `AUDIENCE` is where that is encoded rather than in the producer.
    """
    await world.confirmed()

    await world.settle()

    recipients = {
        row["payload"]["recipient_id"] for row in await world.queued(Notification.REVIEW_REQUESTED)
    }
    assert recipients == {str(world.mentee)}
    assert str(world.mentor) not in recipients


async def test_a_session_nobody_attended_asks_nothing(world: World) -> None:
    """**Structurally unreachable, not merely unlikely.**

    The sweep settles this as `no_show`, and the producer reads the settled
    status — so the wrong message has no path to being sent. That is decision
    10's argument for firing on the transition rather than on a clock: a timer
    would have fired before the sweep decided anything.
    """
    session_id = await world.confirmed()
    await world.nobody_came(session_id)

    await world.settle()

    assert await world.queued(Notification.REVIEW_REQUESTED) == []


async def test_a_recent_review_of_the_same_offering_suppresses_the_request(
    world: World,
) -> None:
    """Decision 8: the producer and the write refuse on one predicate.

    Asking for something `POST /reviews` would then refuse is the disagreement
    that predicate exists to prevent — and it is worse than useless, because the
    mentee acts on the request and is turned away.
    """
    earlier = await world.confirmed(hours_ago=5)
    await world.settle()
    await world.review(earlier)

    later = await world.confirmed(hours_ago=4)
    await world.settle()

    asked = {row["entity_id"] for row in await world.queued(Notification.REVIEW_REQUESTED)}
    assert later not in asked


async def test_a_different_offering_is_still_asked_about(world: World) -> None:
    """The accepting half. Without it a producer that suppressed everything
    would pass the test above."""
    first = await world.confirmed(hours_ago=5)
    await world.settle()
    await world.review(first)

    other = await add_session_type(world.engine, world.mentor, name=f"Interview {uuid4().hex[:6]}")
    later = await world.confirmed(offering=other, hours_ago=4)
    await world.settle()

    asked = {row["entity_id"] for row in await world.queued(Notification.REVIEW_REQUESTED)}
    assert later in asked


async def test_settling_twice_asks_once(world: World) -> None:
    """The sweep is driven by an external scheduler, and *"an external scheduler
    is the kind that fires twice"* — its own docstring. The second run settles
    nothing, so it produces nothing."""
    await world.confirmed()

    await world.settle()
    await world.settle()

    assert len(await world.queued(Notification.REVIEW_REQUESTED)) == 1


# --------------------------------------------------------------------------
# The nudge
# --------------------------------------------------------------------------


async def test_the_nudge_fires_while_the_review_is_still_owed(world: World) -> None:
    await world.confirmed()
    await world.settle()

    nudged = await world.nudge()

    queued = await world.queued(Notification.REVIEW_REQUESTED)
    assert nudged == 1
    assert [row["payload"].get("kind") for row in queued] == [None, REVIEW_REMINDER_KIND]


async def test_a_written_review_makes_the_nudge_a_no_op(world: World) -> None:
    """**This is what "cancelled by the review existing" means.**

    Nothing is unscheduled. The callback asks whether the review exists and does
    nothing if it does, which makes the state unreachable rather than handled —
    the same design the response and session reminders already use.
    """
    session_id = await world.confirmed()
    await world.settle()
    await world.review(session_id)

    nudged = await world.nudge()

    queued = await world.queued(Notification.REVIEW_REQUESTED)
    assert nudged == 0
    assert [row["payload"].get("kind") for row in queued] == [None]


async def test_the_nudge_carries_its_kind(world: World) -> None:
    """**Without a `kind` the dedup index does not apply.**

    `uq_outbox_events_reminder` is partial on `payload ? 'kind'`, so a reminder
    that omits one sits outside the index entirely and `ON CONFLICT DO NOTHING`
    silently stops protecting it — a QStash retry would then send a second
    identical email.
    """
    await world.confirmed()
    await world.settle()

    await world.nudge()

    repeat = (await world.queued(Notification.REVIEW_REQUESTED))[1]["payload"]
    assert repeat["kind"] == REVIEW_REMINDER_KIND
    assert repeat["interval"] == REVIEW_REMINDER_INTERVAL


async def test_two_deliveries_of_the_same_nudge_send_once(world: World) -> None:
    """QStash retries by design, so the second call must be a no-op rather than
    a second identical email."""
    await world.confirmed()
    await world.settle()

    await world.nudge()
    await world.nudge()

    queued = await world.queued(Notification.REVIEW_REQUESTED)
    assert [row["payload"].get("kind") for row in queued] == [None, REVIEW_REMINDER_KIND]


async def test_a_session_never_asked_about_is_never_nudged(world: World) -> None:
    """**The suppression comes free, and this is what proves it.**

    The sweep reads the `REVIEW_REQUESTED` row, so a session whose request the
    interval suppressed has nothing to nudge from. Anchoring on the settlement
    instead would have needed the interval rule restated here — a second copy of
    the question "is a review wanted".
    """
    earlier = await world.confirmed(hours_ago=5)
    await world.settle()
    await world.review(earlier)

    suppressed = await world.confirmed(hours_ago=4)
    await world.settle()

    assert suppressed not in {
        row["entity_id"] for row in await world.queued(Notification.REVIEW_REQUESTED)
    }
    assert await world.nudge() == 0


async def test_a_session_that_is_no_longer_completed_is_not_nudged(
    world: World, db_engine: AsyncEngine
) -> None:
    """Two conditions, not one. A settlement can be corrected, and a reminder
    about a session that is no longer finished is a message about nothing."""
    session_id = await world.confirmed()
    await world.settle()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'cancelled' WHERE id = :i"), {"i": session_id}
        )

    assert await world.nudge() == 0


async def test_the_request_and_the_settlement_share_a_transaction(
    world: World, db_engine: AsyncEngine
) -> None:
    """**A message about a settlement that rolled back cannot exist.**

    `settle_attendance` does not commit, so a caller that rolls back takes the
    request with it — which is why the enqueue is inside the sweep rather than
    after it.
    """
    await world.confirmed()
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        db_engine, expire_on_commit=False
    )

    async with factory() as db:
        await settle_attendance(db, now=dt.datetime.now(dt.UTC) + JOIN_CLOSES)
        await db.rollback()

    assert await world.queued(Notification.REVIEW_REQUESTED) == []


async def test_the_nudge_does_not_nudge_itself(world: World) -> None:
    """**The loop the anchor exists to prevent.**

    The repeat is a `REVIEW_REQUESTED` row like the first ask, so a sweep reading
    *every* row of that type would find its own nudge a day later and ask again —
    and again, for as long as the review went unwritten. It anchors on the row
    with **no** `kind`, which is the original by construction.
    """
    await world.confirmed()
    await world.settle()

    first = await world.nudge()
    again = await world.nudge()
    third = await world.nudge()

    assert (first, again, third) == (1, 0, 0)
    assert len(await world.queued(Notification.REVIEW_REQUESTED)) == 2


async def test_the_first_ask_carries_no_interval(world: World) -> None:
    """**Absence is what marks it**, so the template branches on that rather than
    on a second message type. One template, asked twice."""
    await world.confirmed()

    await world.settle()

    payload = (await world.queued(Notification.REVIEW_REQUESTED))[0]["payload"]
    assert "interval" not in payload
    assert "kind" not in payload

"""The one-off analytics history backfill (PRD 30 PR 7).

A small history is written straight into the tables, at known times, and the
runner hands its events to a fake sink. Each expected event is what the live
writer would have queued for that fact (`test_analytics_events.py`), plus a
`uuid` derived from the fact, so PostHog drops a fact sent twice.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from tests.integration.factories import add_session_type, make_public_mentor
from tests.integration.test_api_admin_credits import a_user

from app.core.errors import UpstreamError
from app.domain.analytics_events import HISTORY_NAMESPACE
from app.infra.analytics_backfill import BackfillRefusedError, backfill_analytics
from app.infra.db import analytics_backfill as facts
from app.infra.db.demo_seed import DEMO_DOMAIN

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

T0 = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.UTC)
CUTOFF = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)
#: The PostHog project a run sends to (`target_fingerprint`).
TARGET = "a" * 64


def at(hours: float) -> dt.datetime:
    return T0 + dt.timedelta(hours=hours)


@dataclass
class FakeSink:
    """Takes every batch, or refuses from the ``fail_on``-th call."""

    batches: list[list[dict[str, Any]]] = field(default_factory=list)
    fail_on: int | None = None

    def capture(self, events: list[dict[str, Any]]) -> None:
        if self.fail_on is not None and len(self.batches) + 1 >= self.fail_on:
            raise UpstreamError("analytics capture failed: refused")
        self.batches.append(list(events))

    @property
    def sent(self) -> list[dict[str, Any]]:
        """Every event as it left, person properties (decision 340) and all."""
        return [event for batch in self.batches for event in batch]

    @property
    def events(self) -> list[dict[str, Any]]:
        """Every event without its person's properties: the fact's own event,
        which the tests below compare with the live writer's."""
        return [
            {**e, "properties": {k: v for k, v in e["properties"].items() if k not in PERSON}}
            for e in self.sent
        ]


PERSON = ("$set", "$set_once")


def expected(
    event: str, fact: Any, user: Any, when: dt.datetime, **properties: Any
) -> dict[str, Any]:
    return {
        "event": event,
        "distinct_id": str(user),
        "timestamp": when.isoformat(),
        "properties": {
            **{
                k: None if v is None else str(v) if isinstance(v, UUID) else v
                for k, v in properties.items()
            },
            "$geoip_disable": True,
        },
        "uuid": str(uuid5(HISTORY_NAMESPACE, f"{event}:{fact}")),
    }


# --------------------------------------------------------------------------
# The history
# --------------------------------------------------------------------------


async def session_row(
    conn: AsyncConnection,
    mentor: UUID,
    mentee: UUID,
    session_type: UUID,
    status: str,
    *,
    day: int,
    moved_from: UUID | None = None,
) -> UUID:
    return (
        await conn.execute(
            text(
                "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
                "duration_minutes, status, rescheduled_from_session_id) "
                "VALUES (:m, :e, :t, :s, 45, :st, :r) RETURNING id"
            ),
            {
                "m": mentor,
                "e": mentee,
                "t": session_type,
                "s": T0 + dt.timedelta(days=60 + day),
                "st": status,
                "r": moved_from,
            },
        )
    ).scalar_one()


async def session_event(
    conn: AsyncConnection,
    session_id: UUID,
    from_status: str | None,
    to_status: str,
    when: dt.datetime,
    *,
    actor: UUID | None = None,
    reason_code: str | None = None,
) -> UUID:
    return (
        await conn.execute(
            text(
                "INSERT INTO session_events (session_id, from_status, to_status, actor_id, "
                "actor_type, reason_code, created_at) "
                "VALUES (:s, :f, :t, :a, :at, :r, :w) RETURNING id"
            ),
            {
                "s": session_id,
                "f": from_status,
                "t": to_status,
                "a": actor,
                "at": "user" if actor else "system",
                "r": reason_code,
                "w": when,
            },
        )
    ).scalar_one()


async def credit(
    conn: AsyncConnection,
    user: UUID,
    when: dt.datetime,
    *,
    source: str = "monthly_free",
    granted: int = 3,
    spent_on: UUID | None = None,
) -> tuple[UUID, UUID | None]:
    """A lot granted at ``when``, and its spend on ``spent_on`` an hour later."""
    lot = (
        await conn.execute(
            text(
                "INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining) "
                "VALUES (:u, :s, :q, :q) RETURNING id"
            ),
            {"u": user, "s": source, "q": granted},
        )
    ).scalar_one()
    grant = await ledger(conn, user, lot, granted, "grant", None, when)
    spend = None
    if spent_on is not None:
        spend = await ledger(
            conn, user, lot, -1, "session_booked", spent_on, when + dt.timedelta(hours=1)
        )
    return grant, spend


async def ledger(
    conn: AsyncConnection,
    user: UUID,
    lot: UUID,
    delta: int,
    reason: str,
    session_id: UUID | None,
    when: dt.datetime,
) -> UUID:
    return (
        await conn.execute(
            text(
                "INSERT INTO credit_transactions "
                "(user_id, credit_lot_id, delta, reason, session_id, created_at) "
                "VALUES (:u, :l, :d, :r, :s, :w) RETURNING id"
            ),
            {"u": user, "l": lot, "d": delta, "r": reason, "s": session_id, "w": when},
        )
    ).scalar_one()


async def status(conn: AsyncConnection, mentor: UUID, status_type: str, when: dt.datetime) -> UUID:
    return (
        await conn.execute(
            text(
                "INSERT INTO mentor_status_events (mentor_user_id, status_type, created_at) "
                "VALUES (:m, :t, :w) RETURNING id"
            ),
            {"m": mentor, "t": status_type, "w": when},
        )
    ).scalar_one()


def received(
    fact: Any, mentor: Any, when: dt.datetime, session_id: Any, session_type: Any
) -> dict[str, Any]:
    """The mentor's side of a request (decision 337)."""
    return expected(
        "booking_received", fact, mentor, when, session_id=session_id, session_type_id=session_type
    )


@dataclass
class History:
    expected: list[dict[str, Any]]


async def a_history(engine: AsyncEngine) -> History:
    """One of each fact, in time order, and the facts the backfill must skip."""
    mentor = await make_public_mentor(engine, f"bf-{uuid4()}", listed=False)
    session_type = await add_session_type(engine, mentor)
    mentee = await a_user(engine)
    admin = await a_user(engine, role="limited_access")
    gone = await a_user(engine, deleted=True)
    demo = await a_user(engine)
    invitee = await a_user(engine)
    grandfathered = await a_user(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET email = :e WHERE id = :u"),
            {"e": f"bf-{uuid4()}@{DEMO_DOMAIN}", "u": demo},
        )
    want: list[dict[str, Any]] = []
    ev = expected

    async with engine.begin() as conn:
        # A request, accepted, then held.
        s1 = await session_row(conn, mentor, mentee, session_type, "completed", day=1)
        made = await session_event(conn, s1, None, "pending_mentor_approval", at(1), actor=mentee)
        want.append(
            ev(
                "booking_requested",
                made,
                mentee,
                at(1),
                session_id=s1,
                session_type_id=session_type,
            )
        )
        want.append(received(made, mentor, at(1), s1, session_type))
        accepted = await session_event(
            conn, s1, "pending_mentor_approval", "confirmed", at(2), actor=mentor
        )
        want.append(ev("session_confirmed", accepted, mentee, at(2), session_id=s1))
        held = await session_event(conn, s1, "confirmed", "completed", at(3))
        want.append(ev("session_completed", held, mentee, at(3), session_id=s1))
        want.append(ev("session_delivered", held, mentor, at(3), session_id=s1))

        # Booked outright, paid for, then cancelled by the mentee.
        s2 = await session_row(conn, mentor, mentee, session_type, "cancelled", day=2)
        made = await session_event(conn, s2, None, "confirmed", at(4), actor=mentee)
        want.append(
            ev(
                "booking_requested",
                made,
                mentee,
                at(4),
                session_id=s2,
                session_type_id=session_type,
            )
        )
        want.append(received(made, mentor, at(4), s2, session_type))
        want.append(ev("session_confirmed", made, mentee, at(4), session_id=s2))
        cancelled = await session_event(
            conn, s2, "confirmed", "cancelled", at(5), actor=mentee, reason_code="other"
        )
        want.append(
            ev(
                "session_cancelled",
                cancelled,
                mentee,
                at(5),
                session_id=s2,
                by="mentee",
                reason_code="other",
            )
        )

        # Confirmed, moved (nothing about the move), then missed.
        s3 = await session_row(conn, mentor, mentee, session_type, "cancelled", day=3)
        made = await session_event(conn, s3, None, "confirmed", at(6), actor=mentee)
        want.append(
            ev(
                "booking_requested",
                made,
                mentee,
                at(6),
                session_id=s3,
                session_type_id=session_type,
            )
        )
        want.append(received(made, mentor, at(6), s3, session_type))
        want.append(ev("session_confirmed", made, mentee, at(6), session_id=s3))
        await session_event(
            conn, s3, "confirmed", "cancelled", at(7), actor=mentee, reason_code="rescheduled"
        )
        s4 = await session_row(conn, mentor, mentee, session_type, "no_show", day=4, moved_from=s3)
        await session_event(conn, s4, None, "confirmed", at(7), actor=mentee)
        missed = await session_event(conn, s4, "confirmed", "no_show", at(8))
        want.append(ev("session_no_show", missed, mentee, at(8), session_id=s4))

        # A request moved onto a time confirmed outright: its first confirmation.
        s5 = await session_row(conn, mentor, mentee, session_type, "withdrawn", day=5)
        made = await session_event(conn, s5, None, "pending_mentor_approval", at(9), actor=mentee)
        want.append(
            ev(
                "booking_requested",
                made,
                mentee,
                at(9),
                session_id=s5,
                session_type_id=session_type,
            )
        )
        want.append(received(made, mentor, at(9), s5, session_type))
        await session_event(
            conn,
            s5,
            "pending_mentor_approval",
            "withdrawn",
            at(10),
            actor=mentee,
            reason_code="rescheduled",
        )
        s6 = await session_row(
            conn, mentor, mentee, session_type, "confirmed", day=6, moved_from=s5
        )
        moved = await session_event(conn, s6, None, "confirmed", at(10), actor=mentee)
        want.append(ev("session_confirmed", moved, mentee, at(10), session_id=s6))

        # A grant, and a spend an hour later.
        granted, spent = await credit(conn, mentee, at(11), spent_on=s2)
        want.append(ev("credit_granted", granted, mentee, at(11), amount=3, source="monthly_free"))
        want.append(ev("credit_spent", spent, mentee, at(12), amount=1, session_id=s2))

        # Listed, listed again (no change), unlisted, listed again.
        first = await status(conn, mentor, "listed", at(13))
        want.append(ev("mentor_listed", first, mentor, at(13)))
        await status(conn, mentor, "listed", at(14))
        await status(conn, mentor, "unlisted", at(15))
        again = await status(conn, mentor, "listed", at(16))
        want.append(ev("mentor_listed", again, mentor, at(16)))

        # A review: overall 4, valuable 5, so its value is 4.
        review = (
            await conn.execute(
                text(
                    "INSERT INTO reviews (session_id, reviewed_by, reviewed_for, "
                    "communication_rating, knowledge_rating, practicality_rating, "
                    "support_rating, valuable_rating, overall_rating, nps_recommend_score, "
                    "public_review, created_at) "
                    "VALUES (:s, :a, :m, 3, 3, 3, 3, 5, 4, 9, 'Fine.', :w) RETURNING id"
                ),
                {"s": s1, "a": mentee, "m": mentor, "w": at(17)},
            )
        ).scalar_one()
        want.append(ev("review_submitted", review, mentee, at(17), rating=4))

        # Unlocked by an invite; and a grandfathered unlock, which is no event.
        referral = (
            await conn.execute(
                text(
                    "INSERT INTO referrals (referrer_id, code, invitee_email, invitee_user_id, "
                    "invited_at, signed_up_at, qualified_at) "
                    "VALUES (:r, :c, 'friend@example.com', :i, :w, :w, :w) RETURNING id"
                ),
                {"r": mentee, "c": f"BF{uuid4().hex[:8]}", "i": invitee, "w": at(18)},
            )
        ).scalar_one()
        unlock = (
            await conn.execute(
                text(
                    "INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id, unlocked_at) "
                    "VALUES (:u, :r, :w) RETURNING id"
                ),
                {"u": mentee, "r": referral, "w": at(19)},
            )
        ).scalar_one()
        want.append(ev("referral_unlocked", unlock, mentee, at(19)))
        await conn.execute(
            text(
                "INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id, unlocked_at) "
                "VALUES (:u, NULL, :w)"
            ),
            {"u": grandfathered, "w": at(19)},
        )

        # Left out: an admin's, a demo account's and a deleted account's grants,
        # and the mentee's grants at and after the cutoff.
        for person in (admin, demo, gone):
            await credit(conn, person, at(20))
        await credit(conn, mentee, CUTOFF)
        await credit(conn, mentee, CUTOFF + dt.timedelta(hours=1))

    return History(expected=want)


ORDER = [
    "booking_requested", "booking_received", "session_confirmed", "session_cancelled",
    "session_completed", "session_delivered", "session_no_show", "credit_granted",
    "credit_spent", "mentor_listed", "review_submitted", "referral_unlocked",
]  # fmt: skip


def in_kind_order(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grouped as the runner reads them: sessions, credits, listings, reviews,
    unlocks; each in time order."""
    kind = {
        name: index
        for index, names in enumerate(
            [ORDER[:7], ORDER[7:9], ORDER[9:10], ORDER[10:11], ORDER[11:]]
        )
        for name in names
    }
    return sorted(events, key=lambda e: (kind[e["event"]], e["timestamp"]))


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


async def test_every_fact_before_the_cutoff_is_sent_as_the_live_event(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    history = await a_history(db_engine)
    sink = FakeSink()

    report = await backfill_analytics(
        db_engine, sink=sink, cursor=tmp_path / "cursor.json", until=CUTOFF, target=TARGET
    )

    assert report.cutoff == CUTOFF
    assert sink.events == in_kind_order(history.expected)
    assert report.counts == {
        name: sum(1 for e in history.expected if e["event"] == name) for name in ORDER
    }
    assert report.sent == len(history.expected)


async def test_each_event_carries_its_persons_properties(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The drain's enrichment, read once per batch (decision 340): the mentor
    listed by its history reads its current `listed`, a mentee has no mentor status."""
    await a_history(db_engine)
    sink = FakeSink()

    await backfill_analytics(
        db_engine, sink=sink, cursor=tmp_path / "cursor.json", until=CUTOFF, target=TARGET
    )

    people = {e["distinct_id"]: e["properties"]["$set"] for e in sink.sent}
    assert len(people) > 1
    assert all(person["user_id"] == key for key, person in people.items())
    roles = {person["role"]: person.get("mentor_status") for person in people.values()}
    assert roles == {"mentor": "listed", "mentee": None}
    assert all(PERSON[1] in e["properties"] for e in sink.sent)


async def test_a_dry_run_counts_and_sends_nothing(db_engine: AsyncEngine, tmp_path: Path) -> None:
    history = await a_history(db_engine)
    cursor = tmp_path / "cursor.json"

    report = await backfill_analytics(
        db_engine, sink=None, cursor=cursor, until=CUTOFF, target=TARGET
    )

    assert report.sent == 0
    assert not cursor.exists()
    assert sum(report.counts.values()) == len(history.expected)
    assert report.sample == in_kind_order(history.expected)[:3]


async def test_batches_hold_at_most_the_batch_size(db_engine: AsyncEngine, tmp_path: Path) -> None:
    """Three, the most one fact reports (a request booked outright: requested,
    received, confirmed), since a fact's events are never split."""
    history = await a_history(db_engine)
    sink = FakeSink()

    await backfill_analytics(
        db_engine, sink=sink, cursor=tmp_path / "c.json", batch=3, until=CUTOFF, target=TARGET
    )

    assert all(1 <= len(batch) <= 3 for batch in sink.batches)
    assert sink.events == in_kind_order(history.expected)


async def test_a_stopped_run_resumes_after_what_it_sent(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    history = await a_history(db_engine)
    cursor = tmp_path / "cursor.json"
    stopped = FakeSink(fail_on=3)

    with pytest.raises(UpstreamError):
        await backfill_analytics(
            db_engine, sink=stopped, cursor=cursor, until=CUTOFF, target=TARGET, batch=3
        )
    assert len(stopped.batches) == 2
    assert json.loads(cursor.read_text())["kind"] == "session_events"

    resumed = FakeSink()
    await backfill_analytics(
        db_engine, sink=resumed, cursor=cursor, until=CUTOFF, target=TARGET, batch=3
    )

    # Nothing sent twice, nothing missed.
    assert stopped.events + resumed.events == in_kind_order(history.expected)


async def test_a_cursor_past_a_kind_skips_it(db_engine: AsyncEngine, tmp_path: Path) -> None:
    history = await a_history(db_engine)
    cursor = tmp_path / "cursor.json"
    first = FakeSink()
    await backfill_analytics(db_engine, sink=first, cursor=cursor, until=CUTOFF, target=TARGET)
    saved = json.loads(cursor.read_text())
    assert saved["kind"] == "referral_unlocks"

    again = FakeSink()
    report = await backfill_analytics(
        db_engine, sink=again, cursor=cursor, until=CUTOFF, target=TARGET
    )

    assert again.events == []
    assert report.sent == 0
    assert first.events == in_kind_order(history.expected)


async def test_a_rerun_without_its_cursor_yields_the_same_ids(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    await a_history(db_engine)
    first, second = FakeSink(), FakeSink()

    await backfill_analytics(
        db_engine, sink=first, cursor=tmp_path / "a.json", until=CUTOFF, target=TARGET
    )
    await backfill_analytics(
        db_engine, sink=second, cursor=tmp_path / "b.json", until=CUTOFF, target=TARGET
    )

    assert [e["uuid"] for e in second.events] == [e["uuid"] for e in first.events]
    assert len({e["uuid"] for e in first.events}) == len(first.events)


async def test_an_earlier_until_sends_only_what_is_before_it(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    history = await a_history(db_engine)
    sink = FakeSink()

    report = await backfill_analytics(
        db_engine, sink=sink, cursor=tmp_path / "c.json", until=at(4), target=TARGET
    )

    assert report.cutoff == at(4)
    early = [e for e in history.expected if dt.datetime.fromisoformat(e["timestamp"]) < at(4)]
    assert sink.events == in_kind_order(early)
    assert [e["event"] for e in early] == [
        "booking_requested",
        "booking_received",
        "session_confirmed",
        "session_completed",
        "session_delivered",
    ]


async def test_a_fact_at_until_is_not_sent_and_one_just_before_is(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    mentee = await a_user(db_engine)
    before = CUTOFF - dt.timedelta(microseconds=1)
    async with db_engine.begin() as conn:
        sent, _ = await credit(conn, mentee, before)
        await credit(conn, mentee, CUTOFF)
    sink = FakeSink()

    await backfill_analytics(
        db_engine, sink=sink, cursor=tmp_path / "c.json", until=CUTOFF, target=TARGET
    )

    assert [e for e in sink.events if e["distinct_id"] == str(mentee)] == [
        expected("credit_granted", sent, mentee, before, amount=3, source="monthly_free")
    ]


async def test_a_legacy_confirmation_is_sent_at_the_requests_time(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The legacy import writes a creation event and a final one, never the
    acceptance: a session that ended from `confirmed` with no event into it was
    still confirmed, so it reports `session_confirmed`, dated by the request
    (Bubble never recorded when it was accepted)."""
    mentor = await make_public_mentor(db_engine, f"bf-{uuid4()}", listed=False)
    session_type = await add_session_type(db_engine, mentor)
    mentee = await a_user(db_engine)
    async with db_engine.begin() as conn:
        legacy = await session_row(conn, mentor, mentee, session_type, "completed", day=7)
        made = await session_event(
            conn, legacy, None, "pending_mentor_approval", at(1), actor=mentee
        )
        held = await session_event(conn, legacy, "confirmed", "completed", at(5))
    sink = FakeSink()

    await backfill_analytics(
        db_engine, sink=sink, cursor=tmp_path / "c.json", until=CUTOFF, target=TARGET
    )

    assert [e for e in sink.events if e["distinct_id"] == str(mentee)] == [
        expected(
            "booking_requested",
            made,
            mentee,
            at(1),
            session_id=legacy,
            session_type_id=session_type,
        ),
        expected("session_confirmed", held, mentee, at(1), session_id=legacy),
        expected("session_completed", held, mentee, at(5), session_id=legacy),
    ]


@pytest.mark.parametrize("left_out", ["mentee", "mentor"])
async def test_a_session_is_sent_for_each_side_whose_person_is_counted(
    db_engine: AsyncEngine, tmp_path: Path, left_out: str
) -> None:
    """A deleted mentee's session still was the mentor's, and an internal
    mentor's still the mentee's: each side goes only for its own person."""
    mentor = await make_public_mentor(db_engine, f"bf-{uuid4()}", listed=False)
    session_type = await add_session_type(db_engine, mentor)
    mentee = await a_user(db_engine, deleted=left_out == "mentee")
    if left_out == "mentor":
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET email = :e WHERE id = :u"),
                {"e": f"bf-{uuid4()}@{DEMO_DOMAIN}", "u": mentor},
            )
    async with db_engine.begin() as conn:
        s1 = await session_row(conn, mentor, mentee, session_type, "completed", day=8)
        made = await session_event(conn, s1, None, "confirmed", at(1), actor=mentee)
        held = await session_event(conn, s1, "confirmed", "completed", at(2))
    sink = FakeSink()

    await backfill_analytics(
        db_engine, sink=sink, cursor=tmp_path / "c.json", until=CUTOFF, target=TARGET
    )

    sent = [e for e in sink.events if e["distinct_id"] in (str(mentor), str(mentee))]
    if left_out == "mentee":
        assert sent == [
            received(made, mentor, at(1), s1, session_type),
            expected("session_delivered", held, mentor, at(2), session_id=s1),
        ]
    else:
        assert sent == [
            expected(
                "booking_requested",
                made,
                mentee,
                at(1),
                session_id=s1,
                session_type_id=session_type,
            ),
            expected("session_confirmed", made, mentee, at(1), session_id=s1),
            expected("session_completed", held, mentee, at(2), session_id=s1),
        ]


async def test_the_cursor_records_its_cutoff_and_target(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    await a_history(db_engine)
    cursor = tmp_path / "cursor.json"

    await backfill_analytics(db_engine, sink=FakeSink(), cursor=cursor, until=CUTOFF, target=TARGET)

    saved = json.loads(cursor.read_text())
    assert saved["until"] == CUTOFF.isoformat()
    assert saved["target"] == TARGET


@pytest.mark.parametrize(
    ("until", "target", "named"),
    [
        (CUTOFF + dt.timedelta(hours=1), TARGET, "--until"),
        (CUTOFF, "b" * 64, "PostHog"),
    ],
)
async def test_a_cursor_for_another_cutoff_or_target_is_refused(
    db_engine: AsyncEngine, tmp_path: Path, until: dt.datetime, target: str, named: str
) -> None:
    await a_history(db_engine)
    cursor = tmp_path / "cursor.json"
    await backfill_analytics(db_engine, sink=FakeSink(), cursor=cursor, until=CUTOFF, target=TARGET)
    again = FakeSink()

    with pytest.raises(BackfillRefusedError, match=named):
        await backfill_analytics(db_engine, sink=again, cursor=cursor, until=until, target=target)
    assert again.events == []


async def test_the_session_event_reader_pages_through_ties_in_order(
    db_engine: AsyncEngine,
) -> None:
    """One row a page, every row on the same instant: each comes once, by id."""
    mentor = await make_public_mentor(db_engine, f"bf-{uuid4()}", listed=False)
    session_type = await add_session_type(db_engine, mentor)
    mentee = await a_user(db_engine)
    tie = dt.datetime(2019, 3, 1, 9, 0, tzinfo=dt.UTC) + dt.timedelta(
        microseconds=uuid4().int % 1_000_000
    )
    async with db_engine.begin() as conn:
        made = []
        for day in range(4):
            row = await session_row(
                conn, mentor, mentee, session_type, "pending_mentor_approval", day=day
            )
            made.append(
                await session_event(conn, row, None, "pending_mentor_approval", tie, actor=mentee)
            )
    before = (tie - dt.timedelta(microseconds=1), UUID(int=2**128 - 1))

    async with AsyncSession(db_engine) as session:
        read = [
            row["id"]
            async for row in facts.session_event_facts(
                session, until=tie + dt.timedelta(microseconds=1), after=before, page=1
            )
        ]

    assert read == sorted(made)

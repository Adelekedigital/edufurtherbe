"""The ten server-side analytics events (PRD 30 PR 5), each queued by the writer
of the fact it reports, in that fact's transaction.

Every test drives the real writer, through the API where one exists, and reads
the `outbox_events` rows with `destination = 'analytics'`: the event, who it is
about (`distinct_id`), when, and its properties. An internal account's fact
(an admin's, a demo account's) queues nothing, by the admin Analytics rule.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_session, make_public_mentor, token_for
from tests.integration.test_api_admin_credits import a_user
from tests.integration.test_api_admin_credits import grant as admin_grant
from tests.integration.test_api_reviews import World, world  # noqa: F401 - world is a fixture
from tests.integration.test_api_session_move import a_booking as a_movable_booking
from tests.integration.test_api_session_move import move
from tests.integration.test_api_session_transitions import a_booking, url
from tests.integration.test_monthly_grant import LADDER, run_grant
from tests.integration.test_monthly_grant import a_user as a_level_mentee
from tests.integration.test_referral_unlock_attended import (
    a_session_attended_by,
    an_onboarded_referrer,
    qualify_an_invitee,
)

from app.domain.analytics_events import GEOIP_DISABLE, AnalyticsEvent
from app.domain.enums import MentorStatusType
from app.infra.db import outbox
from app.infra.db.credit_grants import top_up_levels
from app.infra.db.demo_seed import DEMO_DOMAIN
from app.infra.db.mentor_status_store import decide, record
from app.infra.db.outbox import enqueue_analytics_each
from app.infra.db.session_writer.attendance import settle_attendance
from conftest import A_REASON, bearer, default_ladder

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

AT = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.UTC)


async def events(engine: AsyncEngine, event: str | None = None) -> list[dict[str, Any]]:
    """The analytics payloads queued, oldest first, of one ``event`` or all."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT event_type, entity_id, payload FROM outbox_events "
                "WHERE destination = 'analytics' AND (CAST(:e AS text) IS NULL OR event_type = :e) "
                "ORDER BY created_at, id"
            ),
            {"e": event},
        )
        rows = [dict(row) for row in found.mappings()]
    for row in rows:
        # The row and its payload say the same thing.
        assert row["event_type"] == row["payload"]["event"]
        assert str(row["entity_id"]) == row["payload"]["distinct_id"]
        assert row["payload"]["properties"].pop(GEOIP_DISABLE) is True
    return [row["payload"] for row in rows]


def when(payload: dict[str, Any]) -> dt.datetime:
    return dt.datetime.fromisoformat(payload["timestamp"])


def about(payloads: list[dict[str, Any]], session_id: Any) -> list[dict[str, Any]]:
    return [p for p in payloads if p["properties"].get("session_id") == str(session_id)]


# --------------------------------------------------------------------------
# Booking, confirming, cancelling
# --------------------------------------------------------------------------


async def test_an_auto_confirmed_booking_is_requested_confirmed_and_paid(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    before = dt.datetime.now(dt.UTC)
    booking = await a_booking(db_engine, api_client, "ev-auto", confirmed=True)
    after = dt.datetime.now(dt.UTC)
    mentee = str(booking["mentee_id"])
    async with db_engine.connect() as conn:
        session_type = (
            await conn.execute(
                text("SELECT session_type_id FROM sessions WHERE id = :i"), {"i": booking["id"]}
            )
        ).scalar_one()

    (requested,) = await events(db_engine, "booking_requested")
    (confirmed,) = await events(db_engine, "session_confirmed")
    (spent,) = await events(db_engine, "credit_spent")

    assert requested["distinct_id"] == confirmed["distinct_id"] == spent["distinct_id"] == mentee
    assert requested["properties"] == {
        "session_id": booking["id"],
        "session_type_id": str(session_type),
    }
    assert confirmed["properties"] == {"session_id": booking["id"]}
    assert spent["properties"] == {"amount": 1, "session_id": booking["id"]}
    for payload in (requested, confirmed, spent):
        assert before <= when(payload) <= after


async def test_a_request_is_not_confirmed_until_the_mentor_accepts(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "ev-accept")

    assert len(await events(db_engine, "booking_requested")) == 1
    assert await events(db_engine, "session_confirmed") == []

    before = dt.datetime.now(dt.UTC)
    accepted = await api_client.post(url(booking, "accept"), headers=booking["mentor"])
    assert accepted.status_code == 200, accepted.text

    (confirmed,) = await events(db_engine, "session_confirmed")
    assert confirmed["distinct_id"] == str(booking["mentee_id"])
    assert confirmed["properties"] == {"session_id": booking["id"]}
    assert before <= when(confirmed) <= dt.datetime.now(dt.UTC)


@pytest.mark.parametrize("party", ["mentee", "mentor"])
async def test_a_cancellation_names_who_cancelled_and_why(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, party: str
) -> None:
    booking = await a_booking(db_engine, api_client, f"ev-cancel-{party}", confirmed=True)

    before = dt.datetime.now(dt.UTC)
    cancelled = await api_client.post(url(booking, "cancel"), json=A_REASON, headers=booking[party])
    assert cancelled.status_code == 200, cancelled.text

    (event,) = await events(db_engine, "session_cancelled")
    assert event["distinct_id"] == str(booking["mentee_id"])
    assert event["properties"] == {
        "session_id": booking["id"],
        "by": party,
        "reason_code": A_REASON["reason_code"],
    }
    assert before <= when(event) <= dt.datetime.now(dt.UTC)


async def test_a_declined_request_is_neither_confirmed_nor_cancelled(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "ev-decline")

    declined = await api_client.post(
        url(booking, "decline"), json=A_REASON, headers=booking["mentor"]
    )
    assert declined.status_code == 200, declined.text

    assert await events(db_engine, "session_confirmed") == []
    assert await events(db_engine, "session_cancelled") == []


async def test_a_move_is_not_a_new_booking_a_confirmation_or_a_cancellation(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_movable_booking(api_client, db_engine, "ev-move")
    assert booking["status"] == "confirmed"
    queued = await events(db_engine)

    moved = await move(api_client, booking, booking["starts"][1])
    assert moved.status_code == 201, moved.text

    # Exactly what the original booking queued, and nothing about the move.
    assert await events(db_engine) == queued
    assert {p["event"] for p in queued} == {
        "booking_requested",
        "session_confirmed",
        "credit_spent",
    }


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------


async def test_settlement_reports_each_outcome_about_its_mentee(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "ev-settle")
    held = await add_session(db_engine, mentor, status="confirmed", mentee_attendance="attended")
    missed = await add_session(
        db_engine,
        mentor,
        status="confirmed",
        attendance="pending",
        mentee_attendance="attended",
        days_ago=2,
    )
    async with db_engine.connect() as conn:
        mentees = dict(
            (
                await conn.execute(
                    text("SELECT id, mentee_id FROM sessions WHERE id IN (:a, :b)"),
                    {"a": held, "b": missed},
                )
            ).all()
        )

    async with async_sessionmaker(db_engine)() as session:
        assert await settle_attendance(session, now=AT, ladder=default_ladder()) == 2
        await session.commit()

    (completed,) = await events(db_engine, "session_completed")
    (no_show,) = await events(db_engine, "session_no_show")
    assert completed == {
        "event": "session_completed",
        "distinct_id": str(mentees[held]),
        "timestamp": AT.isoformat(),
        "properties": {"session_id": str(held)},
    }
    assert no_show == {
        "event": "session_no_show",
        "distinct_id": str(mentees[missed]),
        "timestamp": AT.isoformat(),
        "properties": {"session_id": str(missed)},
    }


# --------------------------------------------------------------------------
# Credits, listing, reviews, referrals
# --------------------------------------------------------------------------


async def test_an_admin_grant_is_granted_credit_about_its_recipient(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin_auth = uuid4()
    await a_user(db_engine, admin_auth, role="limited_access")
    first, second = await a_user(db_engine), await a_user(db_engine)

    before = dt.datetime.now(dt.UTC)
    granted = await admin_grant(api_client, admin_auth, [first, second], quantity=2)
    assert granted.status_code == 200, granted.text
    assert granted.json()["granted"] == [str(first), str(second)]

    found = await events(db_engine, "credit_granted")
    assert sorted(p["distinct_id"] for p in found) == sorted([str(first), str(second)])
    for payload in found:
        assert payload["properties"] == {"amount": 2, "source": "admin_grant"}
        assert before <= when(payload) <= dt.datetime.now(dt.UTC)


async def test_a_listing_is_mentor_listed_at_the_events_own_time(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "ev-listed", listed=False)

    async with async_sessionmaker(db_engine)() as session:
        assert await record(
            session, user_id=mentor, status_type=MentorStatusType.LISTED, created_by=None
        )
        await session.commit()
    async with db_engine.connect() as conn:
        written = (
            await conn.execute(
                text(
                    "SELECT created_at FROM mentor_status_events "
                    "WHERE mentor_user_id = :m AND status_type = 'listed'"
                ),
                {"m": mentor},
            )
        ).scalar_one()

    (listed,) = await events(db_engine, "mentor_listed")
    assert listed["distinct_id"] == str(mentor)
    assert listed["properties"] == {}
    assert when(listed) == written


async def test_an_unlisting_is_not_a_listing(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "ev-unlisted")

    async with async_sessionmaker(db_engine)() as session:
        assert await record(
            session, user_id=mentor, status_type=MentorStatusType.UNLISTED, created_by=None
        )
        await session.commit()

    assert await events(db_engine) == []


async def test_a_review_is_submitted_by_its_reviewer_with_its_rating(
    world: World,  # noqa: F811 - the fixture imported above
) -> None:
    session_id = await world.completed(world.offering_a)

    before = dt.datetime.now(dt.UTC)
    # Overall 4 and valuable 5 (`BODY`): the value is the overall rating.
    written = await world.review(session_id)
    assert written.status_code == 201, written.text

    (review,) = await events(world.engine, "review_submitted")
    assert review["distinct_id"] == str(world.mentee)
    assert review["properties"] == {"rating": 4}
    assert before <= when(review) <= dt.datetime.now(dt.UTC)


async def test_an_unlock_is_referral_unlocked_about_the_referrer(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, referrer_id = await an_onboarded_referrer(api_client, db_engine)
    await a_session_attended_by(db_engine, referrer_id, status="completed", mentor_came=True)
    assert await events(db_engine, "referral_unlocked") == []

    before = dt.datetime.now(dt.UTC)
    await qualify_an_invitee(api_client, db_engine, referrer)

    (unlocked,) = await events(db_engine, "referral_unlocked")
    assert unlocked["distinct_id"] == str(referrer_id)
    assert unlocked["properties"] == {}
    assert before <= when(unlocked) <= dt.datetime.now(dt.UTC)


# --------------------------------------------------------------------------
# The transaction, and who is left out
# --------------------------------------------------------------------------


async def test_an_event_rolls_back_with_its_fact(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "ev-rollback", listed=False)

    async with async_sessionmaker(db_engine)() as session:
        assert await record(
            session, user_id=mentor, status_type=MentorStatusType.LISTED, created_by=None
        )
        assert len(await events_in(session)) == 1
        await session.rollback()

    assert await events(db_engine) == []


async def events_in(session: Any) -> list[Any]:
    return list(
        (
            await session.execute(
                text("SELECT id FROM outbox_events WHERE destination = 'analytics'")
            )
        ).all()
    )


async def test_an_admins_fact_queues_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin_auth = uuid4()
    await a_user(db_engine, admin_auth, role="limited_access")
    other_admin = await a_user(db_engine, role="limited_access")
    former_admin = await a_user(db_engine, role="limited_access", revoked=True)
    mentee = await a_user(db_engine)

    granted = await admin_grant(api_client, admin_auth, [other_admin, former_admin, mentee])
    assert granted.status_code == 200, granted.text

    # A revoked grant is no longer an admin: counted, like anybody else.
    found = await events(db_engine, "credit_granted")
    assert sorted(p["distinct_id"] for p in found) == sorted([str(former_admin), str(mentee)])


async def test_a_demo_accounts_fact_queues_nothing(db_engine: AsyncEngine) -> None:
    demo = await make_public_mentor(db_engine, "ev-demo", listed=False)
    real = await make_public_mentor(db_engine, "ev-real", listed=False)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET email = :e WHERE id = :u"),
            {"e": f"mentor-ev-demo@{DEMO_DOMAIN}", "u": demo},
        )

    async with async_sessionmaker(db_engine)() as session:
        for mentor in (demo, real):
            assert await record(
                session, user_id=mentor, status_type=MentorStatusType.LISTED, created_by=None
            )
        await session.commit()

    assert [p["distinct_id"] for p in await events(db_engine, "mentor_listed")] == [str(real)]


async def test_many_facts_in_one_statement_keep_their_own_people(db_engine: AsyncEngine) -> None:
    """Many facts in one statement each keep their own person and properties."""
    people = [uuid4() for _ in range(3)]
    async with async_sessionmaker(db_engine)() as session:
        await enqueue_analytics_each(
            session,
            AnalyticsEvent.CREDIT_GRANTED,
            [(user, {"amount": n + 1, "source": "admin_grant"}) for n, user in enumerate(people)],
            at=AT,
        )
        await session.commit()

    found = {UUID(p["distinct_id"]): p["properties"]["amount"] for p in await events(db_engine)}
    assert found == {user: n + 1 for n, user in enumerate(people)}


async def test_many_facts_span_chunks_without_losing_one(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five facts in chunks of two: three statements, every row written."""
    monkeypatch.setattr(outbox, "ANALYTICS_CHUNK", 2)
    people = [uuid4() for _ in range(5)]
    async with async_sessionmaker(db_engine)() as session:
        await enqueue_analytics_each(
            session,
            AnalyticsEvent.CREDIT_GRANTED,
            [(user, {"amount": n + 1, "source": "admin_grant"}) for n, user in enumerate(people)],
            at=AT,
        )
        await session.commit()

    found = {UUID(p["distinct_id"]): p["properties"]["amount"] for p in await events(db_engine)}
    assert found == {user: n + 1 for n, user in enumerate(people)}


# --------------------------------------------------------------------------
# Only news: a listing, a move, a grant
# --------------------------------------------------------------------------


async def test_listing_a_listed_mentor_again_is_not_a_second_listing(
    db_engine: AsyncEngine,
) -> None:
    mentor = await make_public_mentor(db_engine, "ev-relist", listed=False)

    for _ in range(2):
        async with async_sessionmaker(db_engine)() as session:
            assert await record(
                session, user_id=mentor, status_type=MentorStatusType.LISTED, created_by=None
            )
            await session.commit()

    assert [p["distinct_id"] for p in await events(db_engine, "mentor_listed")] == [str(mentor)]


async def test_approving_an_approved_listed_mentor_lists_nobody(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "ev-reapprove")
    admin = await make_public_mentor(db_engine, "ev-reapprove-admin")

    async with async_sessionmaker(db_engine)() as session:
        assert await decide(session, user_id=mentor, admin_id=admin, approved=True)
        await session.commit()

    assert await events(db_engine, "mentor_listed") == []


async def test_approving_an_unlisted_applicant_lists_them_once(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "ev-approve", approved=False, listed=False)
    admin = await make_public_mentor(db_engine, "ev-approve-admin")

    async with async_sessionmaker(db_engine)() as session:
        assert await decide(session, user_id=mentor, admin_id=admin, approved=True)
        await session.commit()

    assert [p["distinct_id"] for p in await events(db_engine, "mentor_listed")] == [str(mentor)]


async def confirmation_off(engine: AsyncEngine, mentor: UUID, *, on: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = :c WHERE user_id = :u"
            ),
            {"c": on, "u": mentor},
        )


async def test_a_request_moved_onto_a_confirmed_time_is_confirmed_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_movable_booking(
        api_client, db_engine, "ev-move-pending", mentor_confirms=True
    )
    assert booking["status"] == "pending_mentor_approval"
    await confirmation_off(db_engine, booking["mentor"], on=False)

    moved = await move(api_client, booking, booking["starts"][1])
    assert moved.status_code == 201, moved.text
    assert moved.json()["status"] == "confirmed"

    (confirmed,) = await events(db_engine, "session_confirmed")
    assert confirmed["distinct_id"] == str(booking["mentee"])
    assert confirmed["properties"] == {"session_id": moved.json()["id"]}
    assert len(await events(db_engine, "booking_requested")) == 1


async def test_a_confirmed_session_moved_to_a_request_is_reconfirmed_on_accept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The stated assumption: accepting the moved request is a re-confirmation."""
    booking = await a_movable_booking(api_client, db_engine, "ev-move-reconfirm")
    assert booking["status"] == "confirmed"
    await confirmation_off(db_engine, booking["mentor"], on=True)

    moved = await move(api_client, booking, booking["starts"][1])
    assert moved.status_code == 201, moved.text
    assert moved.json()["status"] == "pending_mentor_approval"
    assert len(await events(db_engine, "session_confirmed")) == 1

    mentor = bearer(await token_for(db_engine, booking["mentor"]))
    accepted = await api_client.post(url(moved.json(), "accept"), headers=mentor)
    assert accepted.status_code == 200, accepted.text

    first, again = await events(db_engine, "session_confirmed")
    assert first["properties"] == {"session_id": booking["id"]}
    assert again["properties"] == {"session_id": moved.json()["id"]}


async def lot_sources(engine: AsyncEngine, user: UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT quantity_granted, source FROM credit_lots WHERE user_id = :u"),
            {"u": user},
        )
        return [{"amount": row[0], "source": row[1]} for row in found.all()]


async def test_the_firsts_grant_and_a_top_up_name_their_source(db_engine: AsyncEngine) -> None:
    paid_monthly = await a_level_mentee(db_engine)

    assert await run_grant(db_engine, now=AT) == 1
    (monthly,) = await events(db_engine, "credit_granted")
    assert monthly["distinct_id"] == str(paid_monthly)
    assert [monthly["properties"]] == await lot_sources(db_engine, paid_monthly)
    assert monthly["properties"]["source"] == "monthly_free"

    topped = await a_level_mentee(db_engine)
    async with async_sessionmaker(db_engine)() as session:
        paid = await top_up_levels(session, [topped], ladder=LADDER, now=AT)
        await session.commit()

    assert set(paid) == {topped}
    found = await events(db_engine, "credit_granted")
    (top_up,) = [p for p in found if p["distinct_id"] == str(topped)]
    assert top_up["properties"] == {"amount": paid[topped], "source": "level_top_up"}

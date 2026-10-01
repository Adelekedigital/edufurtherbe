"""A self-paused mentor's return date, and the one reminder it earns.

Calendar request item 1 (2026-10-01): pause may carry `return_on`, a date in the
mentor's own zone; the read says `paused_by_mentor` so only a self-pause shows
the Busy UI; and on the return morning the mentor is reminded, never relisted.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.test_mentor_status_log import ADMIN, add_mentor, make_user

from app.core.config import Settings
from app.infra.db.mentor_status_store import decide, pause, remind_returning_mentors
from app.infra.db.outbox import drain
from app.infra.jobs import runner
from app.infra.jobs.runner import RuntimeJobs
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ZONE = "Pacific/Auckland"


async def a_mentor(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    auth_id = uuid4()
    mentor = await make_user(engine, auth_id, f"{tag}@example.com")
    await add_mentor(engine, mentor, approved=True)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = :z WHERE id = :u"), {"z": ZONE, "u": mentor}
        )
    return mentor, bearer(api_token(auth_id))


async def headers_of(engine: AsyncEngine, user_id: UUID) -> dict[str, str]:
    async with engine.connect() as conn:
        auth_id = (
            await conn.execute(text("SELECT auth_id FROM users WHERE id = :u"), {"u": user_id})
        ).scalar_one()
    return bearer(api_token(auth_id))


def local_today() -> dt.date:
    return dt.datetime.now(ZoneInfo(ZONE)).date()


def pause_url(mentor: UUID) -> str:
    return f"/api/v1/users/{mentor}/mentor-profile/pause"


async def profile(
    client: httpx.AsyncClient, mentor: UUID, headers: dict[str, str]
) -> dict[str, Any]:
    response = await client.get(f"/api/v1/users/{mentor}/mentor-profile", headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def unlistings(engine: AsyncEngine, mentor: UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM mentor_status_events "
                        "WHERE mentor_user_id = :u AND status_type = 'unlisted'"
                    ),
                    {"u": mentor},
                )
            ).scalar_one()
        )


# --------------------------------------------------------------------------
# Pausing with a date
# --------------------------------------------------------------------------


async def test_a_pause_carries_its_return_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await a_mentor(db_engine, "with-date")
    back = local_today() + dt.timedelta(days=7)

    response = await api_client.post(
        pause_url(mentor), json={"return_on": back.isoformat()}, headers=headers
    )
    read = await profile(api_client, mentor, headers)

    assert response.status_code == 200, response.text
    assert read["paused_by_mentor"] is True
    assert read["return_on"] == back.isoformat()
    assert read["listing_status"] == "unlisted"


async def test_a_pause_without_a_body_is_not_sure_yet(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await a_mentor(db_engine, "no-body")

    response = await api_client.post(pause_url(mentor), headers=headers)
    read = await profile(api_client, mentor, headers)

    assert response.status_code == 200, response.text
    assert (read["paused_by_mentor"], read["return_on"]) == (True, None)


@pytest.mark.parametrize("days", [0, -1])
async def test_a_return_date_must_be_after_the_mentors_today(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, days: int
) -> None:
    """Their today, in their zone: UTC's would refuse a mentor ahead of it."""
    mentor, headers = await a_mentor(db_engine, f"too-soon-{days}")
    when = local_today() + dt.timedelta(days=days)

    response = await api_client.post(
        pause_url(mentor), json={"return_on": when.isoformat()}, headers=headers
    )

    assert response.status_code == 422, response.text
    assert any(e["pointer"] == "/return_on" for e in response.json()["errors"])
    assert (await profile(api_client, mentor, headers))["paused_by_mentor"] is False


async def test_pausing_again_changes_only_the_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Changing the return date is pause again, and records no second transition."""
    mentor, headers = await a_mentor(db_engine, "again")
    first = local_today() + dt.timedelta(days=3)
    second = local_today() + dt.timedelta(days=10)
    await api_client.post(pause_url(mentor), json={"return_on": first.isoformat()}, headers=headers)

    response = await api_client.post(
        pause_url(mentor), json={"return_on": second.isoformat()}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert (await profile(api_client, mentor, headers))["return_on"] == second.isoformat()
    assert await unlistings(db_engine, mentor) == 1


async def test_resuming_clears_the_return_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await a_mentor(db_engine, "resume")
    back = local_today() + dt.timedelta(days=2)
    await api_client.post(pause_url(mentor), json={"return_on": back.isoformat()}, headers=headers)

    resumed = await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume", headers=headers
    )
    read = await profile(api_client, mentor, headers)

    assert resumed.status_code == 200, resumed.text
    assert (read["listing_status"], read["paused_by_mentor"], read["return_on"]) == (
        "listed",
        False,
        None,
    )


async def test_an_admins_unlisting_is_not_a_pause_on_the_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "the-admin-read@example.com", role="super_admin")
    mentor, headers = await a_mentor(db_engine, "admin-read")
    await api_client.post(
        f"{ADMIN}/mentors/{mentor}/listing",
        params={"listed": "false"},
        json={"reason": "admin_review"},
        headers=bearer(api_token(admin_auth)),
    )

    read = await profile(api_client, mentor, headers)

    assert (read["listing_status"], read["paused_by_mentor"]) == ("unlisted", False)


# --------------------------------------------------------------------------
# The return-morning reminder
# --------------------------------------------------------------------------


async def paused_until(
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, back: dt.date
) -> UUID:
    """A self-paused mentor whose return date is `back`, set directly so a test
    may use a fixed calendar date."""
    mentor, headers = await a_mentor(engine, tag)
    await client.post(pause_url(mentor), headers=headers)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET return_on = :d WHERE user_id = :u"),
            {"d": back, "u": mentor},
        )
    return mentor


def local(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=ZoneInfo(ZONE))


async def sweep(engine: AsyncEngine, now: dt.datetime) -> int:
    async with AsyncSession(engine) as session:
        sent = await remind_returning_mentors(session, now=now)
        await session.commit()
    return sent


async def reminders(engine: AsyncEngine, mentor: UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'mentor_return_reminder' AND entity_id = :u"
                    ),
                    {"u": mentor},
                )
            ).scalar_one()
        )


async def test_the_return_morning_queues_one_reminder(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    back = dt.date(2026, 10, 10)
    mentor = await paused_until(db_engine, api_client, "remind", back)

    early = await sweep(db_engine, local(back, 7, 59))
    due = await sweep(db_engine, local(back, 8))
    again = await sweep(db_engine, local(back, 9))

    assert (early, due, again) == (0, 1, 0)
    assert await reminders(db_engine, mentor) == 1


async def test_a_resumed_mentor_is_not_reminded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    back = dt.date(2026, 10, 11)
    mentor = await paused_until(db_engine, api_client, "resumed", back)
    resumed = await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume",
        headers=await headers_of(db_engine, mentor),
    )
    assert resumed.status_code == 200, resumed.text

    assert await sweep(db_engine, local(back, 9)) == 0
    assert await reminders(db_engine, mentor) == 0


async def test_an_admin_unlisted_mentor_is_not_reminded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The date can outlive the pause when an admin unlists over it; the
    reminder must not, since there is nothing the mentor can switch back on."""
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "the-admin-remind@example.com", role="super_admin")
    back = dt.date(2026, 10, 12)
    mentor = await paused_until(db_engine, api_client, "admin-remind", back)
    await api_client.post(
        f"{ADMIN}/mentors/{mentor}/listing",
        params={"listed": "false"},
        json={"reason": "admin_review"},
        headers=bearer(api_token(admin_auth)),
    )

    assert await sweep(db_engine, local(back, 9)) == 0
    assert await reminders(db_engine, mentor) == 0


async def test_a_new_date_rearms_the_reminder(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    back = dt.date(2026, 10, 13)
    mentor = await paused_until(db_engine, api_client, "rearm", back)
    assert await sweep(db_engine, local(back, 8)) == 1
    later = local_today() + dt.timedelta(days=5)
    await api_client.post(
        pause_url(mentor),
        json={"return_on": later.isoformat()},
        headers=await headers_of(db_engine, mentor),
    )

    assert await sweep(db_engine, local(later, 8)) == 1
    assert await reminders(db_engine, mentor) == 2


async def test_a_pending_applicant_is_not_reminded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Resume needs approval, so a pending applicant who paused with a date is
    not told to switch back on: the action in the message is not theirs."""
    auth_id = uuid4()
    mentor = await make_user(db_engine, auth_id, "pending-remind@example.com")
    await add_mentor(db_engine, mentor)
    await api_client.post(pause_url(mentor), headers=bearer(api_token(auth_id)))
    back = dt.date(2026, 10, 14)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET return_on = :d WHERE user_id = :u"),
            {"d": back, "u": mentor},
        )
        await conn.execute(
            text("UPDATE users SET timezone = :z WHERE id = :u"), {"z": ZONE, "u": mentor}
        )

    assert await sweep(db_engine, local(back, 9)) == 0
    assert await reminders(db_engine, mentor) == 0


async def test_a_listing_written_any_way_ends_the_return_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The projection trigger clears it, so even an event inserted directly,
    as a migration or an operator might, leaves no stale date behind."""
    back = dt.date(2026, 10, 15)
    mentor = await paused_until(db_engine, api_client, "direct-list", back)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_status_events (mentor_user_id, status_type) "
                "VALUES (:u, 'listed')"
            ),
            {"u": mentor},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT listing_status, return_on, return_reminded_at "
                    "FROM mentor_profiles WHERE user_id = :u"
                ),
                {"u": mentor},
            )
        ).one()

    assert tuple(row) == ("listed", None, None)


async def test_a_claimed_reminder_survives_a_send_that_fails_its_commit(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Claimed and queued before anything is sent.** If the run dies after the
    provider accepted the email but before it committed, a single transaction
    would roll the marker and the outbox row back, and the retried run would
    queue and send the reminder again under a new idempotency key."""
    back = local_today() - dt.timedelta(days=1)
    mentor = await paused_until(db_engine, api_client, "durable", back)

    async def sent_then_lost(*_: Any, **__: Any) -> dict[str, int]:
        raise RuntimeError("the provider accepted it, then the run died")

    monkeypatch.setattr(runner, "drain", sent_then_lost)
    jobs = RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database)))
    with pytest.raises(RuntimeError):
        await jobs.run("settle-sessions")

    async with db_engine.connect() as conn:
        reminded = (
            await conn.execute(
                text("SELECT return_reminded_at FROM mentor_profiles WHERE user_id = :u"),
                {"u": mentor},
            )
        ).scalar_one()
    assert reminded is not None, "the claim was rolled back with the failed run"
    assert await reminders(db_engine, mentor) == 1


class Recorder:
    """A notifier that sends nothing and remembers what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, **kwargs: Any) -> None:
        self.sent.append(str(kwargs["notification"]))


async def drained(
    engine: AsyncEngine, migrated_database: str, now: dt.datetime | None = None
) -> list[str]:
    notifier = Recorder()
    async with AsyncSession(engine) as session:
        await drain(
            session,
            notifier=notifier,
            now=now or dt.datetime.now(dt.UTC),
            settings=Settings(_env_file=None, database_url=SecretStr(migrated_database)),
        )
        await session.commit()
    return [n for n in notifier.sent if n == "mentor_return_reminder"]


async def test_a_queued_reminder_still_due_is_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """The positive half of the send-time check."""
    back = local_today() - dt.timedelta(days=1)
    await paused_until(db_engine, api_client, "still-due", back)
    assert await sweep(db_engine, dt.datetime.now(dt.UTC)) == 1

    assert await drained(db_engine, migrated_database) == ["mentor_return_reminder"]


async def test_a_queued_reminder_is_not_sent_after_a_resume(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Queued, then the mentor came back before it went out: telling a listed
    mentor to switch back on would be false."""
    back = local_today() - dt.timedelta(days=1)
    mentor = await paused_until(db_engine, api_client, "resumed-queued", back)
    assert await sweep(db_engine, dt.datetime.now(dt.UTC)) == 1
    await api_client.post(
        f"/api/v1/users/{mentor}/mentor-profile/resume",
        headers=await headers_of(db_engine, mentor),
    )

    assert await drained(db_engine, migrated_database) == []


async def test_a_queued_reminder_is_not_sent_for_an_old_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Queued for one date, then the mentor moved it: the old one is stale."""
    back = local_today() - dt.timedelta(days=1)
    mentor = await paused_until(db_engine, api_client, "moved-queued", back)
    assert await sweep(db_engine, dt.datetime.now(dt.UTC)) == 1
    later = local_today() + dt.timedelta(days=9)
    await api_client.post(
        pause_url(mentor),
        json={"return_on": later.isoformat()},
        headers=await headers_of(db_engine, mentor),
    )

    assert await drained(db_engine, migrated_database) == []


async def test_a_deleted_account_cannot_be_paused(db_engine: AsyncEngine) -> None:
    """Soft-deleted after the caller was authenticated: nothing to write to."""
    mentor = await make_user(db_engine, uuid4(), "deleted-pause@example.com")
    await add_mentor(db_engine, mentor, approved=True)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    async with AsyncSession(db_engine) as session:
        outcome = await pause(session, user_id=mentor, now=dt.datetime.now(dt.UTC))
        await session.commit()

    assert outcome == "absent"
    assert await unlistings(db_engine, mentor) == 0


async def test_the_pause_publishes_its_conflict() -> None:
    """The generated client must have a branch for the admin-unlisted refusal."""
    from app.main import create_app

    spec = create_app(Settings(_env_file=None)).openapi()
    operation = next(
        ops["post"] for path, ops in spec["paths"].items() if path.endswith("/mentor-profile/pause")
    )

    assert "409" in operation["responses"]


async def test_a_zone_moved_after_queueing_waits_for_the_new_morning(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Queued on the Auckland morning, then the mentor moved to Honolulu, where
    it is still the day before: it waits for their new morning, not sent early
    and not dropped."""
    back = dt.date(2026, 10, 20)
    mentor = await paused_until(db_engine, api_client, "moved-zone", back)
    queued_at = local(back, 9)
    assert await sweep(db_engine, queued_at) == 1
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = 'Pacific/Honolulu' WHERE id = :u"), {"u": mentor}
        )

    early = await drained(db_engine, migrated_database, queued_at)
    honolulu_morning = dt.datetime.combine(back, dt.time(8), tzinfo=ZoneInfo("Pacific/Honolulu"))
    later = await drained(db_engine, migrated_database, honolulu_morning)

    assert early == []
    assert later == ["mentor_return_reminder"]


async def test_a_decision_on_a_deleted_account_is_absent(db_engine: AsyncEngine) -> None:
    """An account deleted before the decision locks it: absent, not a decision
    that reports success, records nothing and queues a message anyway."""
    admin = await make_user(db_engine, uuid4(), "decider@example.com", role="super_admin")
    mentor = await make_user(db_engine, uuid4(), "decided-deleted@example.com")
    await add_mentor(db_engine, mentor)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    async with AsyncSession(db_engine) as session:
        decided = await decide(session, user_id=mentor, admin_id=admin, approved=True)
        await session.commit()

    async with db_engine.connect() as conn:
        queued = (
            await conn.execute(
                text("SELECT count(*) FROM outbox_events WHERE entity_id = :u"), {"u": mentor}
            )
        ).scalar_one()
    assert decided is False
    assert queued == 0


async def test_a_deleted_accounts_queued_reminder_is_dropped_not_kept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Moved to a zone where it is still early, then the account was deleted:
    stale, not waiting, so it is not re-selected ahead of live mail forever."""
    back = dt.date(2026, 10, 21)
    mentor = await paused_until(db_engine, api_client, "deleted-queued", back)
    queued_at = local(back, 9)
    assert await sweep(db_engine, queued_at) == 1
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET timezone = 'Pacific/Honolulu', deleted_at = now() WHERE id = :u"
            ),
            {"u": mentor},
        )

    await drained(db_engine, migrated_database, queued_at)

    async with db_engine.connect() as conn:
        status = (
            await conn.execute(
                text(
                    "SELECT status FROM outbox_events "
                    "WHERE event_type = 'mentor_return_reminder' AND entity_id = :u"
                ),
                {"u": mentor},
            )
        ).scalar_one()
    assert status == "skipped"

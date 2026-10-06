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
from sqlalchemy import Integer, literal, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.test_mentor_status_log import ADMIN, add_mentor, make_user

from app.core.config import Settings
from app.domain.listing import RETURN_REMINDER_HOUR
from app.domain.messages import build_variables
from app.infra.db.mentor_listing import stage_due
from app.infra.db.mentor_status_store import decide, pause, remind_returning_mentors
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User
from app.infra.db.outbox import drain
from app.infra.jobs import runner
from app.infra.jobs.runner import RuntimeJobs
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ZONE = "Pacific/Auckland"


async def a_mentor(engine: AsyncEngine, tag: str, zone: str = ZONE) -> tuple[UUID, dict[str, str]]:
    """A paused-capable approved mentor, in `zone`.

    **`zone` exists for the tests that write a timestamp with the database's
    clock and then assert against the process's** — see `midday_zone`. Everything
    else keeps `ZONE`, where the awkward offset is the point.
    """
    auth_id = uuid4()
    mentor = await make_user(engine, auth_id, f"{tag}@example.com")
    await add_mentor(engine, mentor, approved=True)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = :z WHERE id = :u"), {"z": zone, "u": mentor}
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
    engine: AsyncEngine, client: httpx.AsyncClient, tag: str, back: dt.date, stage: int = 0
) -> UUID:
    """A self-paused mentor whose return date is `back` with `stage` pending
    (the day-of by default), set directly so a test may use a fixed date."""
    mentor, headers = await a_mentor(engine, tag)
    await client.post(pause_url(mentor), headers=headers)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET return_on = :d, return_reminder_stage = :s "
                "WHERE user_id = :u"
            ),
            {"d": back, "s": stage, "u": mentor},
        )
    return mentor


def local(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=ZoneInfo(ZONE))


def midday_zone(at: dt.datetime | None = None) -> str:
    """A fixed-offset zone where it is 12:00 now, for a test that reads two clocks.

    **This is what makes a real-clock date test deterministic.** `ZONE` is
    `Pacific/Auckland`, UTC+13 in October, so its date rolls over at 11:00 UTC.
    A test that writes an event with the *database's* `now()` and then computes
    an expected date from the *process's* clock has those two reads on either
    side of that boundary whenever the suite happens to run across it — and the
    two dates then differ by one day.

    `stage_missed` compares dates, not instants: `due.date() < local_now.date()`.
    So one day of disagreement turns a reminder that is due today into one whose
    day has gone, the send is skipped, and the test sees `0` where it expects
    `1` — for a few minutes a day, with no code change involved.

    Twelve hundred local keeps every date read nine or more hours from either
    midnight, so no boundary exists for the two clocks to straddle. The same
    trick as `afternoon_zone`, which exists for the sibling case of needing the
    local hour to be past `RETURN_REMINDER_HOUR`.

    Found when it failed CI on an unrelated PR and passed on a bare re-run
    (#372). A test that goes green on a re-run teaches everybody to re-run.
    """
    return _fixed_offset_zone(12, at=at or dt.datetime.now(dt.UTC))


def _fixed_offset_zone(local_hour: int, *, at: dt.datetime) -> str:
    """An `Etc/GMT±n` zone in which it is `local_hour` at the instant `at`.

    The sign is inverted in those names — `Etc/GMT-3` is UTC+3 — which is why
    this is written once rather than at each call site.

    **`at` is required, not defaulted.** This read the clock itself until a
    review pointed out the obvious: a caller that then reads the clock again to
    check the local hour has two readings, and a UTC hour boundary between them
    makes the offset stale. That is the same two-clock defect this whole change
    exists to remove, reintroduced inside the guard meant to prove it gone.
    Taking the instant makes a second reading impossible rather than unlikely.
    """
    offset = local_hour - at.hour
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def afternoon_zone(at: dt.datetime | None = None) -> str:
    """A fixed-offset zone where it is 14:00 now: a test on the real clock then
    finds today's stage due (after 08:00) and its day not yet gone."""
    return _fixed_offset_zone(14, at=at or dt.datetime.now(dt.UTC))


async def test_the_midday_zone_is_far_from_any_date_boundary() -> None:
    """**The property the real-clock date tests rest on** (#372).

    Those tests compare a date they computed against one the database derived.
    That is only safe while no date boundary can fall between the two reads, and
    this is what keeps it true: at noon local, midnight is nine or more hours
    away in both directions, so no plausible gap between two clock reads can
    cross one.

    `async` with nothing awaited, because this module marks every test
    `asyncio` and a synchronous one under that mark warns — and this project
    turns that warning into an error, which `failure-modes.md` records as having
    hidden a flood once.

    Asserted rather than assumed, because the fix is otherwise invisible — a
    later edit to the hour, or to the inverted `Etc/GMT` sign, would silently
    put the tests back on a boundary and they would fail for a few minutes a
    day with no code change in sight. Watched to fail by changing the hour to 0.
    """
    # **One reading, both derived from it.** Calling the helper and then reading
    # the clock again is two readings, and a UTC hour boundary between them makes
    # the offset stale — the defect this change removes, which a review caught
    # here after I had written it into the guard itself.
    moment = dt.datetime.now(dt.UTC)
    local = moment.astimezone(ZoneInfo(midday_zone(moment)))

    assert local.hour == 12
    hours_to_midnight = min(local.hour, 24 - local.hour)
    assert hours_to_midnight >= 9, f"only {hours_to_midnight}h from a date rollover"


async def test_the_afternoon_zone_is_past_the_reminder_hour() -> None:
    """`afternoon_zone`'s own documented property, pinned because it now shares
    an implementation with `midday_zone` — a change to that helper could satisfy
    one caller and break the other, and this is the half that would go quiet:
    a stage not yet due simply does not send, which looks like a different bug.
    """
    moment = dt.datetime.now(dt.UTC)
    local = moment.astimezone(ZoneInfo(afternoon_zone(moment)))

    assert local.hour == 14
    assert local.hour > RETURN_REMINDER_HOUR, "today's stage would not be due yet"
    # And its day must not have gone, or the send is dropped as missed.
    assert local.hour < 24


async def due_now(engine: AsyncEngine, client: httpx.AsyncClient, tag: str) -> UUID:
    """A self-paused mentor whose day-of reminder is due by the real clock."""
    zone = afternoon_zone()
    mentor = await paused_until(engine, client, tag, dt.datetime.now(ZoneInfo(zone)).date())
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = :z WHERE id = :u"), {"z": zone, "u": mentor}
        )
    return mentor


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
                    "SELECT listing_status, return_on, return_reminder_stage "
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
    mentor = await due_now(db_engine, api_client, "durable")

    async def sent_then_lost(*_: Any, **__: Any) -> dict[str, int]:
        raise RuntimeError("the provider accepted it, then the run died")

    monkeypatch.setattr(runner, "drain", sent_then_lost)
    jobs = RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database)))
    with pytest.raises(RuntimeError):
        await jobs.run("settle-sessions")

    async with db_engine.connect() as conn:
        reminded = (
            await conn.execute(
                text("SELECT return_reminder_stage FROM mentor_profiles WHERE user_id = :u"),
                {"u": mentor},
            )
        ).scalar_one()
    assert reminded is None, "the claim was rolled back with the failed run"
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
    await due_now(db_engine, api_client, "still-due")
    assert await sweep(db_engine, dt.datetime.now(dt.UTC)) == 1

    assert await drained(db_engine, migrated_database) == ["mentor_return_reminder"]


async def test_a_queued_reminder_is_not_sent_after_a_resume(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Queued, then the mentor came back before it went out: telling a listed
    mentor to switch back on would be false."""
    mentor = await due_now(db_engine, api_client, "resumed-queued")
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
    mentor = await due_now(db_engine, api_client, "moved-queued")
    assert await sweep(db_engine, dt.datetime.now(dt.UTC)) == 1
    later = dt.date.today() + dt.timedelta(days=9)
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


# --------------------------------------------------------------------------
# Three stages: a week, three days, the day (owner, 2026-10-01)
# --------------------------------------------------------------------------


async def sent_stages(engine: AsyncEngine, mentor: UUID) -> list[str]:
    """The `daysUntilReturn` each queued reminder carries, in order."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT payload->>'stage' FROM outbox_events "
                "WHERE event_type = 'mentor_return_reminder' AND entity_id = :u "
                "ORDER BY created_at"
            ),
            {"u": mentor},
        )
        return [str(r[0]) for r in rows]


async def stage_of(engine: AsyncEngine, mentor: UUID) -> int | None:
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text("SELECT return_reminder_stage FROM mentor_profiles WHERE user_id = :u"),
                {"u": mentor},
            )
        ).scalar_one()
    return None if value is None else int(value)


async def test_a_pause_ten_days_out_sends_each_stage_once_on_its_morning(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await a_mentor(db_engine, "three-stages")
    back = local_today() + dt.timedelta(days=10)
    await api_client.post(pause_url(mentor), json={"return_on": back.isoformat()}, headers=headers)
    week, three = back - dt.timedelta(days=7), back - dt.timedelta(days=3)

    runs = [
        await sweep(db_engine, local(week, 7, 59)),
        await sweep(db_engine, local(week, 8)),
        await sweep(db_engine, local(week, 9)),
        await sweep(db_engine, local(three, 8)),
        await sweep(db_engine, local(three, 9)),
        await sweep(db_engine, local(back, 8)),
        await sweep(db_engine, local(back, 9)),
    ]

    assert runs == [0, 1, 0, 1, 0, 1, 0]
    assert await sent_stages(db_engine, mentor) == ["7", "3", "0"]


async def test_a_two_day_pause_gets_only_the_day_of_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The week and three-day stages are already behind it: no late sends."""
    mentor, headers = await a_mentor(db_engine, "short-pause")
    back = local_today() + dt.timedelta(days=2)
    await api_client.post(pause_url(mentor), json={"return_on": back.isoformat()}, headers=headers)

    assert await stage_of(db_engine, mentor) == 0
    await sweep(db_engine, local(back, 8))
    assert await sent_stages(db_engine, mentor) == ["0"]


async def test_a_new_date_restarts_the_stages_and_drops_the_old_ones(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    mentor, headers = await a_mentor(db_engine, "restart")
    first = local_today() + dt.timedelta(days=10)
    await api_client.post(pause_url(mentor), json={"return_on": first.isoformat()}, headers=headers)
    first_week = first - dt.timedelta(days=7)
    assert await sweep(db_engine, local(first_week, 8)) == 1
    second = local_today() + dt.timedelta(days=12)
    await api_client.post(
        pause_url(mentor), json={"return_on": second.isoformat()}, headers=headers
    )

    stale = await drained(db_engine, migrated_database, local(first_week, 9))
    restarted = await stage_of(db_engine, mentor)
    second_week = second - dt.timedelta(days=7)
    assert await sweep(db_engine, local(second_week, 8)) == 1
    fresh = await drained(db_engine, migrated_database, local(second_week, 9))

    assert stale == []
    assert restarted == 7
    assert fresh == ["mentor_return_reminder"]
    assert (await sent_stages(db_engine, mentor))[-1] == "7"


async def test_a_resume_cancels_every_pending_stage(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    mentor, headers = await a_mentor(db_engine, "cancel-all")
    back = local_today() + dt.timedelta(days=10)
    await api_client.post(pause_url(mentor), json={"return_on": back.isoformat()}, headers=headers)
    week = back - dt.timedelta(days=7)
    assert await sweep(db_engine, local(week, 8)) == 1
    await api_client.post(f"/api/v1/users/{mentor}/mentor-profile/resume", headers=headers)

    assert await stage_of(db_engine, mentor) is None
    assert await drained(db_engine, migrated_database, local(week, 9)) == []
    assert await sweep(db_engine, local(back, 9)) == 0


async def test_a_same_date_re_pause_sends_the_rearmed_stage_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Pausing again with the same date re-arms the stages. A stage queued
    before that must not also go out, or the mentor hears it twice."""
    mentor, headers = await a_mentor(db_engine, "same-date")
    back = local_today() + dt.timedelta(days=10)
    body = {"return_on": back.isoformat()}
    await api_client.post(pause_url(mentor), json=body, headers=headers)
    week = back - dt.timedelta(days=7)
    assert await sweep(db_engine, local(week, 8)) == 1
    await api_client.post(pause_url(mentor), json=body, headers=headers)

    first = await drained(db_engine, migrated_database, local(week, 9))
    assert await sweep(db_engine, local(week, 9)) == 1
    second = await drained(db_engine, migrated_database, local(week, 10))

    assert first + second == ["mentor_return_reminder"]


# --------------------------------------------------------------------------
# A late run sends only the latest due stage (review, 2026-10-01)
# --------------------------------------------------------------------------


async def payload_of(engine: AsyncEngine, mentor: UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = 'mentor_return_reminder' AND entity_id = :u "
                "ORDER BY created_at"
            ),
            {"u": mentor},
        )
        return [dict(r[0]) for r in rows]


async def test_a_run_after_a_gap_sends_only_the_current_stage(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """The week stage was missed; on D-3, after 08:00, the three-day stage is
    due. One email, `daysUntilReturn` 3 — never a late "7 days" then "3 days"."""
    back = local_today() + dt.timedelta(days=10)
    mentor = await paused_until(db_engine, api_client, "gap", back, stage=7)
    late = local(back - dt.timedelta(days=3), 9)

    assert await sweep(db_engine, late) == 1
    assert await sweep(db_engine, local(back - dt.timedelta(days=3), 10)) == 0
    sent = await drained(db_engine, migrated_database, late)

    queued = await payload_of(db_engine, mentor)
    assert sent == ["mentor_return_reminder"]
    assert [p["days_until_return"] for p in queued] == ["3"]
    assert await stage_of(db_engine, mentor) == 0


async def test_a_queued_stage_overtaken_by_a_later_one_is_dropped(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Queued "7 days" still undelivered when "3 days" comes due: stale."""
    back = local_today() + dt.timedelta(days=10)
    mentor = await paused_until(db_engine, api_client, "overtaken", back, stage=7)
    assert await sweep(db_engine, local(back - dt.timedelta(days=7), 8)) == 1

    sent = await drained(db_engine, migrated_database, local(back - dt.timedelta(days=3), 9))

    assert sent == []
    assert await reminders(db_engine, mentor) == 1


# --------------------------------------------------------------------------
# An undated pause: nudges at 30 and 59 days (owner, 2026-10-01)
# --------------------------------------------------------------------------


async def test_an_undated_pause_is_nudged_on_day_thirty_and_day_fifty_nine(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await a_mentor(db_engine, "undated")
    await api_client.post(pause_url(mentor), headers=headers)
    began = local_today()

    assert await stage_of(db_engine, mentor) == 30
    runs = [
        await sweep(db_engine, local(began + dt.timedelta(days=30), 7, 59)),
        await sweep(db_engine, local(began + dt.timedelta(days=30), 8)),
        await sweep(db_engine, local(began + dt.timedelta(days=30), 9)),
        await sweep(db_engine, local(began + dt.timedelta(days=59), 8)),
        await sweep(db_engine, local(began + dt.timedelta(days=59), 9)),
    ]

    assert runs == [0, 1, 0, 1, 0]
    queued = await payload_of(db_engine, mentor)
    assert [p.get("days_paused") for p in queued] == ["30", "59"]
    assert all(p["return_on"] == "" for p in queued)


async def test_a_resume_before_day_thirty_sends_no_nudge(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await a_mentor(db_engine, "undated-resume")
    await api_client.post(pause_url(mentor), headers=headers)
    await api_client.post(f"/api/v1/users/{mentor}/mentor-profile/resume", headers=headers)

    assert await sweep(db_engine, local(local_today() + dt.timedelta(days=30), 9)) == 0
    assert await reminders(db_engine, mentor) == 0


async def test_setting_a_date_switches_to_the_dated_cadence(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Paused with no date, then a date set: 7/3/0 for that date, and no
    30-day nudge any more."""
    mentor, headers = await a_mentor(db_engine, "undated-then-dated")
    await api_client.post(pause_url(mentor), headers=headers)
    back = local_today() + dt.timedelta(days=20)
    await api_client.post(pause_url(mentor), json={"return_on": back.isoformat()}, headers=headers)

    assert await stage_of(db_engine, mentor) == 7
    assert await sweep(db_engine, local(back - dt.timedelta(days=7), 8)) == 1
    # Day 30 is ten days past the date: no 30-day nudge, and no late day-of.
    assert await sweep(db_engine, local(local_today() + dt.timedelta(days=30), 9)) == 0
    queued = await payload_of(db_engine, mentor)
    assert [p.get("days_until_return") for p in queued] == ["7"]
    assert await stage_of(db_engine, mentor) is None


# --------------------------------------------------------------------------
# The due moment, through Python and SQL at the boundary (rule 8)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minute", "armed"),
    [(59, 7), (60, 3)],
    ids=["07:59 arms the week stage", "08:00 has passed it"],
)
async def test_the_due_moment_agrees_in_python_and_sql(
    db_engine: AsyncEngine, minute: int, armed: int
) -> None:
    """Python's "still ahead" and SQL's "due" are each other's negation at
    the instant either side of 08:00 on D-7: the pause (`reminder_due_at`)
    arms the week stage exactly when SQL (`stage_due`) says it is not yet due,
    and a sweep at that instant sends nothing."""
    mentor = await make_user(db_engine, uuid4(), f"boundary-{minute}@example.com")
    await add_mentor(db_engine, mentor, approved=True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = :z WHERE id = :u"), {"z": ZONE, "u": mentor}
        )
    back = dt.date(2026, 11, 20)
    when = local(back - dt.timedelta(days=7), 7) + dt.timedelta(minutes=minute)

    async with AsyncSession(db_engine) as session:
        assert await pause(session, user_id=mentor, now=when, return_on=back) == "paused"
        await session.commit()
        week_due = (
            await session.execute(
                select(stage_due(when, literal(7, Integer)))
                .select_from(MentorProfile)
                .join(User, User.id == MentorProfile.user_id)
                .where(MentorProfile.user_id == mentor)
            )
        ).scalar_one()

    assert await stage_of(db_engine, mentor) == armed
    assert week_due is (armed != 7)
    assert await sweep(db_engine, when) == 0


# --------------------------------------------------------------------------
# A stage whose day has gone is never sent, and the undated anchor (review)
# --------------------------------------------------------------------------


async def test_an_undated_run_on_day_sixty_one_sends_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, headers = await a_mentor(db_engine, "undated-outage")
    await api_client.post(pause_url(mentor), headers=headers)

    assert await sweep(db_engine, local(local_today() + dt.timedelta(days=61), 9)) == 0
    assert await reminders(db_engine, mentor) == 0
    assert await stage_of(db_engine, mentor) is None


async def test_an_undated_run_after_a_gap_sends_only_day_fifty_nine(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Day 30 missed; on day 59, after its 08:00, only the 59 nudge goes."""
    mentor, headers = await a_mentor(db_engine, "undated-gap")
    await api_client.post(pause_url(mentor), headers=headers)

    assert await sweep(db_engine, local(local_today() + dt.timedelta(days=59), 9)) == 1
    assert [p["days_paused"] for p in await payload_of(db_engine, mentor)] == ["59"]
    assert await stage_of(db_engine, mentor) is None


async def status_event_at(
    engine: AsyncEngine, mentor: UUID, *, on: dt.datetime, listed: bool
) -> None:
    """A self-pause or resume written directly, at an explicit moment.

    **`on` rather than `days_ago`, and that is the fix for #372.** This used to
    insert `now() - make_interval(days => :d)` — the *database's* clock — while
    its callers computed the expected reminder date from the *process's*. The
    two reads sat either side of the mentor zone's date rollover whenever the
    suite ran across it, the dates differed by a day, and `stage_missed` dropped
    a reminder that was due.

    Now the caller passes the moment, derived from the one clock read it also
    asserts against, so there are no two readings to disagree.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_status_events "
                "(mentor_user_id, status_type, created_by, reason, created_at) "
                "VALUES (:u, :t, :u, :r, :when)"
            ),
            {
                "u": mentor,
                "t": "listed" if listed else "unlisted",
                "r": None if listed else "mentor_paused",
                "when": on,
            },
        )


async def test_an_undated_re_pause_keeps_the_original_start(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Paused 10 days ago, paused again today: nudged on day 30 of the
    original pause (20 days from now), not 30 days from the re-pause."""
    zone = midday_zone()
    mentor, headers = await a_mentor(db_engine, "undated-repause", zone)
    # One clock read, and everything below is derived from it.
    today = dt.datetime.now(ZoneInfo(zone)).date()
    await status_event_at(
        db_engine,
        mentor,
        on=dt.datetime.combine(today - dt.timedelta(days=10), dt.time(12), ZoneInfo(zone)),
        listed=False,
    )
    await api_client.post(pause_url(mentor), headers=headers)

    assert await stage_of(db_engine, mentor) == 30
    due = dt.datetime.combine(today + dt.timedelta(days=20), dt.time(8), ZoneInfo(zone))
    assert await sweep(db_engine, due) == 1
    assert await unlistings(db_engine, mentor) == 1


async def test_a_resume_then_a_pause_starts_the_count_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Paused 40 days ago, back 20 days ago, paused today: day 30 is from today."""
    zone = midday_zone()
    mentor, headers = await a_mentor(db_engine, "undated-restart", zone)
    today = dt.datetime.now(ZoneInfo(zone)).date()
    for days_ago, listed in ((40, False), (20, True)):
        await status_event_at(
            db_engine,
            mentor,
            on=dt.datetime.combine(
                today - dt.timedelta(days=days_ago), dt.time(12), ZoneInfo(zone)
            ),
            listed=listed,
        )
    await api_client.post(pause_url(mentor), headers=headers)

    assert await stage_of(db_engine, mentor) == 30
    due = dt.datetime.combine(today + dt.timedelta(days=30), dt.time(8), ZoneInfo(zone))
    assert await sweep(db_engine, due) == 1


async def test_a_date_dropped_counts_from_the_original_pause(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Paused 35 days ago, given a date, then "Not sure yet" again: the nudges
    count from 35 days ago, so day 30 is behind and day 59 is next."""
    zone = midday_zone()
    mentor, headers = await a_mentor(db_engine, "dated-then-undated", zone)
    today = dt.datetime.now(ZoneInfo(zone)).date()
    await status_event_at(
        db_engine,
        mentor,
        on=dt.datetime.combine(today - dt.timedelta(days=35), dt.time(12), ZoneInfo(zone)),
        listed=False,
    )
    back = today + dt.timedelta(days=20)
    await api_client.post(pause_url(mentor), json={"return_on": back.isoformat()}, headers=headers)
    assert await stage_of(db_engine, mentor) == 7

    await api_client.post(pause_url(mentor), headers=headers)

    assert await stage_of(db_engine, mentor) == 59


async def test_a_queued_nudge_is_dropped_once_a_date_is_set(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    mentor, headers = await a_mentor(db_engine, "nudge-then-date")
    await api_client.post(pause_url(mentor), headers=headers)
    thirty = local_today() + dt.timedelta(days=30)
    assert await sweep(db_engine, local(thirty, 8)) == 1
    later = local(thirty, 9)
    async with AsyncSession(db_engine) as session:
        await pause(session, user_id=mentor, now=later, return_on=thirty + dt.timedelta(days=10))
        await session.commit()

    sent = await drained(db_engine, migrated_database, later)

    assert sent == []
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


async def test_a_queued_stage_not_sent_on_its_day_is_dropped(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """Queued on its morning, but the drain only ran the next day: dropped."""
    back = local_today() + dt.timedelta(days=10)
    mentor = await paused_until(db_engine, api_client, "queued-day-gone", back, stage=3)
    assert await sweep(db_engine, local(back - dt.timedelta(days=3), 9)) == 1

    assert await drained(db_engine, migrated_database, local(back - dt.timedelta(days=2), 9)) == []
    assert await reminders(db_engine, mentor) == 1


async def test_a_queued_reminder_names_the_mentor_the_template_asks_for(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, migrated_database: str
) -> None:
    """The live template asks only for `mentorName` (Codex on #340): a context
    built without the mentor's name fails every attempt and no paused mentor is
    ever reminded."""
    mentor = await due_now(db_engine, api_client, "named-queued")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET first_name = 'Ngozi', last_name = 'Okafor' WHERE id = :u"),
            {"u": mentor},
        )
    assert await sweep(db_engine, dt.datetime.now(dt.UTC)) == 1
    contexts: list[Any] = []

    class Capture:
        def send(self, **kwargs: Any) -> None:
            if str(kwargs["notification"]) == "mentor_return_reminder":
                contexts.append(kwargs["context"])

    async with AsyncSession(db_engine) as session:
        await drain(
            session,
            notifier=Capture(),
            now=dt.datetime.now(dt.UTC),
            settings=Settings(_env_file=None, database_url=SecretStr(migrated_database)),
        )
        await session.commit()

    (context,) = contexts
    assert build_variables(["mentorName"], context) == {"mentorName": "Ngozi Okafor"}

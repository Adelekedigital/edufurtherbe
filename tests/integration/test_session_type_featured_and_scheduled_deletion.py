"""A mentor features one offering; deleting a booked one schedules it instead.

Session Types frontend round 4 (product owner, 2026-09-29).

**A — featured.** One offering per mentor may be featured; it comes first on
both lists. Featuring another un-features the first in the same transaction,
hiding a featured one un-features it, and a hidden offering or one scheduled for
deletion cannot be featured.

**B — scheduled deletion.** A `DELETE` on an offering with live sessions used
to be a `409`. It now hides the offering and schedules it: the booked sessions
go ahead, `pending_deletion` says when it goes, and the hourly settle run
deletes it once nothing live remains. A restore cancels the schedule and leaves
it hidden.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import add_session_type, make_public_mentor
from tests.integration.test_api_me_session_type_delete import URL, as_mentor, book

from app.core.config import Settings
from app.infra.db.session_type_store import (
    finalise_scheduled_deletions,
    update_session_type,
)
from app.infra.jobs.runner import RuntimeJobs
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def public_url(mentor: UUID) -> str:
    return f"/api/v1/users/{mentor}/session-types"


async def own(client: httpx.AsyncClient, auth: UUID) -> list[dict[str, object]]:
    response = await client.get(URL, headers=bearer(api_token(auth)))
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


async def feature(
    client: httpx.AsyncClient, auth: UUID, session_type: UUID, value: bool = True
) -> httpx.Response:
    return await client.patch(
        f"{URL}/{session_type}", json={"is_featured": value}, headers=bearer(api_token(auth))
    )


async def column(engine: AsyncEngine, session_type: UUID, name: str) -> object:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(f"SELECT {name} FROM session_types WHERE id = :t"),  # noqa: S608 - test-only column name
                {"t": session_type},
            )
        ).scalar_one()


async def set_status(engine: AsyncEngine, session_type: UUID, status: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = :s WHERE session_type_id = :t"),
            {"s": status, "t": session_type},
        )


# --------------------------------------------------------------------------
# A — featured
# --------------------------------------------------------------------------


async def test_the_featured_offering_comes_first_on_both_lists(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-first")
    await add_session_type(db_engine, mentor, name="Alpha")
    beta = await add_session_type(db_engine, mentor, name="Beta")

    response = await feature(api_client, auth, beta)

    assert response.status_code == 200, response.text
    mine = await own(api_client, auth)
    assert [(o["name"], o["is_featured"]) for o in mine] == [("Beta", True), ("Alpha", False)]
    public = (await api_client.get(public_url(mentor))).json()["data"]
    assert [(o["name"], o["is_featured"]) for o in public] == [("Beta", True), ("Alpha", False)]


async def test_with_none_featured_the_order_is_by_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-none")
    await add_session_type(db_engine, mentor, name="Beta")
    await add_session_type(db_engine, mentor, name="Alpha")

    assert [o["name"] for o in await own(api_client, auth)] == ["Alpha", "Beta"]


async def test_featuring_another_unfeatures_the_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-swap")
    alpha = await add_session_type(db_engine, mentor, name="Alpha")
    beta = await add_session_type(db_engine, mentor, name="Beta")
    await feature(api_client, auth, alpha)

    response = await feature(api_client, auth, beta)

    assert response.status_code == 200, response.text
    assert {o["name"]: o["is_featured"] for o in await own(api_client, auth)} == {
        "Alpha": False,
        "Beta": True,
    }


async def test_featuring_is_per_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The un-feature is scoped to the mentor: another mentor's featured offering
    is not theirs to clear."""
    mentor, auth = await as_mentor(db_engine, "feat-mine")
    other, other_auth = await as_mentor(db_engine, "feat-theirs")
    mine = await add_session_type(db_engine, mentor, name="Mine")
    theirs = await add_session_type(db_engine, other, name="Theirs")
    await feature(api_client, other_auth, theirs)

    await feature(api_client, auth, mine)

    assert await column(db_engine, theirs, "is_featured") is True
    assert await column(db_engine, mine, "is_featured") is True


async def test_an_offering_can_be_unfeatured(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-off")
    alpha = await add_session_type(db_engine, mentor, name="Alpha")
    await feature(api_client, auth, alpha)

    response = await feature(api_client, auth, alpha, value=False)

    assert response.status_code == 200, response.text
    assert await column(db_engine, alpha, "is_featured") is False


async def test_a_hidden_offering_cannot_be_featured(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-hidden")
    hidden = await add_session_type(db_engine, mentor, name="Hidden", active=False)

    response = await feature(api_client, auth, hidden)

    assert response.status_code == 422, response.text
    assert [e["pointer"] for e in response.json()["errors"]] == ["/is_featured"]
    assert await column(db_engine, hidden, "is_featured") is False


async def test_featuring_while_showing_in_one_request_is_allowed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The rule is about the offering's state after the write, not before it."""
    mentor, auth = await as_mentor(db_engine, "feat-show")
    hidden = await add_session_type(db_engine, mentor, name="Hidden", active=False)

    response = await api_client.patch(
        f"{URL}/{hidden}",
        json={"is_active": True, "is_featured": True},
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 200, response.text
    assert await column(db_engine, hidden, "is_featured") is True


async def test_hiding_a_featured_offering_unfeatures_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-hide")
    alpha = await add_session_type(db_engine, mentor, name="Alpha")
    await feature(api_client, auth, alpha)
    headers = bearer(api_token(auth))

    hidden = await api_client.patch(f"{URL}/{alpha}", json={"is_active": False}, headers=headers)
    shown = await api_client.patch(f"{URL}/{alpha}", json={"is_active": True}, headers=headers)

    assert hidden.status_code == 200, hidden.text
    assert shown.status_code == 200, shown.text
    assert await column(db_engine, alpha, "is_featured") is False, "showing it re-featured it"


async def test_featuring_and_hiding_in_one_request_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-both")
    alpha = await add_session_type(db_engine, mentor, name="Alpha")

    response = await api_client.patch(
        f"{URL}/{alpha}",
        json={"is_active": False, "is_featured": True},
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 422, response.text
    assert await column(db_engine, alpha, "is_active") is True


async def test_a_null_feature_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "feat-null")
    alpha = await add_session_type(db_engine, mentor, name="Alpha")

    response = await api_client.patch(
        f"{URL}/{alpha}", json={"is_featured": None}, headers=bearer(api_token(auth))
    )

    assert response.status_code == 422, response.text


async def test_another_mentors_offering_cannot_be_featured(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "feat-steal")
    other = await make_public_mentor(db_engine, "feat-victim")
    theirs = await add_session_type(db_engine, other, name="Theirs")

    response = await feature(api_client, auth, theirs)

    assert response.status_code == 404, response.text
    assert await column(db_engine, theirs, "is_featured") is False


async def test_two_features_at_once_serialise(db_engine: AsyncEngine) -> None:
    """**The mentor-wide lock.** Two requests featuring two different offerings
    of one mentor: without it both clear "the others", both set their own, and
    the second commit meets the one-featured index as a 500. With it the second
    waits, then clears the first's — last write wins."""
    mentor, _ = await as_mentor(db_engine, "feat-race")
    alpha = await add_session_type(db_engine, mentor, name="Alpha")
    beta = await add_session_type(db_engine, mentor, name="Beta")

    async with AsyncSession(db_engine) as first, AsyncSession(db_engine) as second:
        await update_session_type(first, mentor, alpha, {"is_featured": True})
        racing = asyncio.create_task(
            update_session_type(second, mentor, beta, {"is_featured": True})
        )
        done, _ = await asyncio.wait({racing}, timeout=1)
        assert not done, "the second feature did not wait for the first"
        await first.commit()
        assert await racing is True
        await second.commit()

    assert await column(db_engine, alpha, "is_featured") is False
    assert await column(db_engine, beta, "is_featured") is True


async def test_the_database_holds_one_featured_and_only_if_active(
    db_engine: AsyncEngine,
) -> None:
    """The index and the `CHECK` stand behind the store: a second writer — a
    script, the seed — cannot put two featured offerings on a mentor, or a
    featured one out of sight."""
    mentor, _ = await as_mentor(db_engine, "feat-db")
    alpha = await add_session_type(db_engine, mentor, name="Alpha")
    beta = await add_session_type(db_engine, mentor, name="Beta")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_types SET is_featured = true WHERE id = :t"), {"t": alpha}
        )

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE session_types SET is_featured = true WHERE id = :t"), {"t": beta}
            )
    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE session_types SET is_active = false WHERE id = :t"), {"t": alpha}
            )


# --------------------------------------------------------------------------
# B — scheduled deletion
# --------------------------------------------------------------------------


async def test_deleting_a_booked_offering_schedules_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "sched")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status="confirmed", days=1)
    await book(db_engine, mentor, booked, status="pending_mentor_approval", days=3)
    # Finished sessions neither hold it open nor count.
    await book(db_engine, mentor, booked, status="completed", days=-3)

    response = await api_client.delete(f"{URL}/{booked}", headers=bearer(api_token(auth)))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["scheduled"] is True
    assert body["booked_count"] == 2
    last = dt.datetime.fromisoformat(body["deletes_after"])
    # The last live session starts three days out and runs 45 minutes.
    expected = dt.datetime.now(dt.UTC) + dt.timedelta(days=3, minutes=45)
    assert abs(last - expected) < dt.timedelta(minutes=5)

    assert await column(db_engine, booked, "deleted_at") is None, "it was deleted outright"
    assert await column(db_engine, booked, "is_active") is False
    (row,) = await own(api_client, auth)
    assert row["is_active"] is False
    assert row["pending_deletion"]["booked_count"] == 2
    assert row["pending_deletion"]["deletes_after"] == body["deletes_after"]
    assert (await api_client.get(public_url(mentor))).json()["data"] == []


@pytest.mark.parametrize("live_status", ["pending_mentor_approval", "confirmed"])
async def test_each_live_status_schedules_rather_than_deletes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, live_status: str
) -> None:
    """One per status, because `LIVE_STATUSES` has two members and losing either
    would delete an offering out from under somebody's plan."""
    mentor, auth = await as_mentor(db_engine, f"sched-{live_status}")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status=live_status)

    response = await api_client.delete(f"{URL}/{booked}", headers=bearer(api_token(auth)))

    assert response.status_code == 202, response.text
    assert await column(db_engine, booked, "deleted_at") is None


async def test_an_unbooked_offering_is_still_deleted_at_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "sched-none")
    spare = await add_session_type(db_engine, mentor, name="Spare")

    response = await api_client.delete(f"{URL}/{spare}", headers=bearer(api_token(auth)))

    assert response.status_code == 204, response.text
    assert await column(db_engine, spare, "deleted_at") is not None


async def test_scheduling_unfeatures_the_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "sched-feat")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await feature(api_client, auth, booked)
    await book(db_engine, mentor, booked, status="confirmed")

    await api_client.delete(f"{URL}/{booked}", headers=bearer(api_token(auth)))

    assert await column(db_engine, booked, "is_featured") is False


async def test_a_second_delete_is_the_same_schedule(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "sched-twice")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status="confirmed")
    headers = bearer(api_token(auth))

    first = await api_client.delete(f"{URL}/{booked}", headers=headers)
    requested = await column(db_engine, booked, "deletion_scheduled_at")
    second = await api_client.delete(f"{URL}/{booked}", headers=headers)

    assert second.status_code == 202, second.text
    assert second.json() == first.json()
    assert await column(db_engine, booked, "deletion_scheduled_at") == requested


async def test_a_scheduled_offering_cannot_be_featured_or_shown(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "sched-show")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status="confirmed")
    headers = bearer(api_token(auth))
    await api_client.delete(f"{URL}/{booked}", headers=headers)

    shown = await api_client.patch(f"{URL}/{booked}", json={"is_active": True}, headers=headers)
    featured = await feature(api_client, auth, booked)

    assert shown.status_code == 422, shown.text
    assert [e["pointer"] for e in shown.json()["errors"]] == ["/is_active"]
    assert featured.status_code == 422, featured.text
    assert await column(db_engine, booked, "is_active") is False


async def test_a_restore_cancels_the_schedule_and_stays_hidden(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "restore")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status="confirmed")
    headers = bearer(api_token(auth))
    await api_client.delete(f"{URL}/{booked}", headers=headers)

    restored = await api_client.post(f"{URL}/{booked}/restore", headers=headers)

    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["id"] == str(booked)
    assert body["pending_deletion"] is None
    assert body["is_active"] is False
    assert await column(db_engine, booked, "deletion_scheduled_at") is None
    shown = await api_client.patch(f"{URL}/{booked}", json={"is_active": True}, headers=headers)
    assert shown.status_code == 200, shown.text


async def test_restoring_an_unscheduled_offering_changes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "restore-noop")
    live = await add_session_type(db_engine, mentor, name="Live")

    response = await api_client.post(f"{URL}/{live}/restore", headers=bearer(api_token(auth)))

    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True
    assert response.json()["pending_deletion"] is None


async def test_another_mentors_offering_cannot_be_restored(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "restore-thief")
    other, other_auth = await as_mentor(db_engine, "restore-owner")
    theirs = await add_session_type(db_engine, other, name="Theirs")
    await book(db_engine, other, theirs, status="confirmed")
    await api_client.delete(f"{URL}/{theirs}", headers=bearer(api_token(other_auth)))

    response = await api_client.post(f"{URL}/{theirs}/restore", headers=bearer(api_token(auth)))

    assert response.status_code == 404, response.text
    assert await column(db_engine, theirs, "deletion_scheduled_at") is not None


async def test_the_settle_run_deletes_only_what_nothing_live_holds(
    db_engine: AsyncEngine,
) -> None:
    """Scheduled and finished goes; scheduled and still booked stays; never
    scheduled stays whatever its sessions. Idempotent: a second run finds none."""
    mentor, _ = await as_mentor(db_engine, "finalise")
    done = await add_session_type(db_engine, mentor, name="Done")
    waiting = await add_session_type(db_engine, mentor, name="Waiting")
    untouched = await add_session_type(db_engine, mentor, name="Untouched")
    await book(db_engine, mentor, done, status="confirmed", days=1)
    await book(db_engine, mentor, waiting, status="confirmed", days=2)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_types SET is_active = false, deletion_scheduled_at = now() "
                "WHERE id IN (:a, :b)"
            ),
            {"a": done, "b": waiting},
        )
    await set_status(db_engine, done, "completed")

    async with AsyncSession(db_engine) as session:
        first = await finalise_scheduled_deletions(session)
        await session.commit()
    async with AsyncSession(db_engine) as session:
        second = await finalise_scheduled_deletions(session)
        await session.commit()

    assert first == 1
    assert second == 0
    assert await column(db_engine, done, "deleted_at") is not None
    assert await column(db_engine, waiting, "deleted_at") is None
    assert await column(db_engine, untouched, "deleted_at") is None


async def test_a_cancelled_booking_moves_the_schedule_up(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Derived, not stored: the figures follow the sessions."""
    mentor, auth = await as_mentor(db_engine, "sched-moves")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status="confirmed")
    await api_client.delete(f"{URL}/{booked}", headers=bearer(api_token(auth)))
    await set_status(db_engine, booked, "cancelled")

    (row,) = await own(api_client, auth)

    assert row["pending_deletion"] == {"booked_count": 0, "deletes_after": None}


async def test_the_hourly_settle_run_finalises_after_settling_attendance(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    """**The wiring and the order, through the job itself.** The only session on
    a scheduled offering is over but still `confirmed` — live until attendance
    is settled. Settled first, it stops holding the offering, and the same run
    deletes it; finalising before settling would leave it for another hour, and
    not calling the finaliser at all would leave it forever."""
    mentor, _ = await as_mentor(db_engine, "sched-job")
    over = await add_session_type(db_engine, mentor, name="Over")
    await book(db_engine, mentor, over, status="confirmed", days=-2)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_types SET is_active = false, deletion_scheduled_at = now() "
                "WHERE id = :t"
            ),
            {"t": over},
        )

    jobs = RuntimeJobs(Settings(_env_file=None, database_url=SecretStr(migrated_database)))
    result = await jobs.run("settle-sessions")

    assert await column(db_engine, over, "deleted_at") is not None
    assert result.counts["deleted_session_types"] == 1


# --------------------------------------------------------------------------
# C — the booked figures on every owner row, so the delete confirm can say
# which of its two things will happen before the mentor chooses
# --------------------------------------------------------------------------


async def test_every_owner_row_carries_its_booked_figures(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "figures")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    free = await add_session_type(db_engine, mentor, name="Free")
    await book(db_engine, mentor, booked, status="confirmed", days=1)
    await book(db_engine, mentor, booked, status="pending_mentor_approval", days=3)
    await book(db_engine, mentor, booked, status="cancelled", days=5)

    rows = {row["id"]: row for row in await own(api_client, auth)}

    held, empty = rows[str(booked)], rows[str(free)]
    assert held["booked_count"] == 2, "the cancelled session was counted"
    last = dt.datetime.fromisoformat(str(held["last_booked_ends_at"]))
    # The last *live* session starts three days out and runs 45 minutes; the
    # cancelled one, later still, must not move it.
    expected = dt.datetime.now(dt.UTC) + dt.timedelta(days=3, minutes=45)
    assert abs(last - expected) < dt.timedelta(minutes=5)
    assert held["pending_deletion"] is None, "nothing was scheduled"
    assert empty["booked_count"] == 0
    assert empty["last_booked_ends_at"] is None


async def test_the_figures_and_the_schedule_agree(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One derivation: what the confirm promised is what the schedule says."""
    mentor, auth = await as_mentor(db_engine, "figures-agree")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status="confirmed", days=2)
    before = (await own(api_client, auth))[0]

    response = await api_client.delete(f"{URL}/{booked}", headers=bearer(api_token(auth)))
    (after,) = await own(api_client, auth)

    assert response.status_code == 202, response.text
    assert before["last_booked_ends_at"] == response.json()["deletes_after"]
    assert after["pending_deletion"] == {
        "booked_count": after["booked_count"],
        "deletes_after": after["last_booked_ends_at"],
    }


async def test_the_public_read_has_no_booked_figures(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """How busy a mentor is stays the mentor's to know."""
    mentor, _ = await as_mentor(db_engine, "figures-public")
    booked = await add_session_type(db_engine, mentor, name="Booked")
    await book(db_engine, mentor, booked, status="confirmed")

    (row,) = (await api_client.get(public_url(mentor))).json()["data"]

    assert "booked_count" not in row
    assert "last_booked_ends_at" not in row

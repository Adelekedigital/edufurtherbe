"""Reconciling the session load: what the loader cannot tell you about itself.

Expected comes from the plan, actual from the database. A check whose two sides
come from the same place agrees with itself and proves nothing — which is why
``SessionLoader`` returns no counts.

**Both identity tests give the plan a source list containing an anchor that is
genuinely absent.** That is not thoroughness, it is the specific gap this
repository has now shipped twice: `_unaccounted` once returned duplicated
anchors while claiming otherwise and every test passed, because none supplied a
source list to be missing from; and PR 66's bookings identity had a term no
fixture reached, so deleting it left the full suite green. A test that cannot
fail is worse than no test, because it retires the suspicion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    MeetingProvider,
    SessionRole,
    SessionStatus,
)
from app.domain.transform.availability import DroppedRow
from app.domain.transform.sessions import (
    ParticipantRow,
    QuarantinedTracker,
    SessionEventRow,
    SessionPlan,
    SessionRow,
    SessionTypeRow,
)
from app.infra.etl.reconcile import reconcile_sessions
from app.infra.etl.sessions import SessionLoader

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MENTOR = "1720393627919x464416579629646660"
MENTEE = "1751061686104x253709313178114300"
BUBBLE_CREATED = datetime(2025, 9, 6, 15, 44, tzinfo=UTC)
BUBBLE_MODIFIED = datetime(2025, 9, 7, 13, 0, tzinfo=UTC)
STARTS_AT = datetime(2025, 9, 8, 5, 0, tzinfo=UTC)


def plan_of(**overrides: object) -> SessionPlan:
    values: dict[str, object] = {
        "sessions": (
            SessionRow(
                legacy_bubble_id="sb-1",
                mentor_bubble_id=MENTOR,
                mentee_bubble_id=MENTEE,
                status=SessionStatus.CANCELLED,
                starts_at=STARTS_AT,
                duration_minutes=45,
                topic=None,
                booking_message=None,
                meeting_provider=MeetingProvider.GOOGLE_MEET,
                meeting_url="https://meet.google.com/ohd-gsnt-rbj",
                external_room_id=None,
                external_calendar_event_id=None,
                created_at=BUBBLE_CREATED,
                updated_at=BUBBLE_MODIFIED,
            ),
        ),
        "participants": (
            ParticipantRow("sb-1", MENTOR, SessionRole.MENTOR, None, AttendanceStatus.NO_SHOW),
            ParticipantRow("sb-1", MENTEE, SessionRole.MENTEE, None, AttendanceStatus.NO_SHOW),
        ),
        "events": (
            SessionEventRow(
                "sb-1",
                None,
                SessionStatus.PENDING_MENTOR_APPROVAL,
                MENTEE,
                ActorType.USER,
                None,
                BUBBLE_CREATED,
            ),
        ),
        "session_types": (SessionTypeRow(MENTOR, "General Mentorship", 45, MeetingProvider.DAILY),),
        "source_booking_ids": ("sb-1",),
    }
    values.update(overrides)
    return SessionPlan(**values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def loaded(db_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, SessionPlan]]:
    async with db_engine.begin() as conn:
        users: dict[str, UUID] = {}
        for bubble_id, email, role in (
            (MENTOR, "mentor@example.test", "mentor"),
            (MENTEE, "mentee@example.test", "mentee"),
        ):
            users[bubble_id] = (
                await conn.execute(
                    text(
                        "INSERT INTO users (email, primary_role, timezone, legacy_bubble_id) "
                        "VALUES (:e, :r, 'Africa/Lagos', :b) RETURNING id"
                    ),
                    {"e": email, "r": role, "b": bubble_id},
                )
            ).scalar_one()
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'M')"),
            {"u": users[MENTOR]},
        )
        plan = plan_of()
        await SessionLoader(conn).load(users=users, plan=plan)
        yield conn, plan


# --------------------------------------------------------------------------


async def test_a_clean_load_reconciles(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    conn, plan = loaded

    result = await reconcile_sessions(conn, plan)

    assert result.ok, result.report()


async def test_a_missing_session_is_caught(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    """The cheapest failure and the one a count alone would also find."""
    conn, plan = loaded
    await conn.execute(text("DELETE FROM session_events"))
    await conn.execute(text("DELETE FROM session_participants"))
    await conn.execute(text("DELETE FROM sessions WHERE legacy_bubble_id = 'sb-1'"))

    result = await reconcile_sessions(conn, plan)

    assert not result.ok
    assert "sb-1" in result.report()


async def test_a_rewritten_timestamp_is_caught(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    """The only check that can detect a load which ran with the trigger enabled.

    Every row present, every count agreeing, every column populated — and every
    `updated_at` quietly moved to the import clock.
    """
    conn, plan = loaded
    await conn.execute(text("UPDATE sessions SET updated_at = now()"))

    result = await reconcile_sessions(conn, plan)

    assert not result.ok
    assert "timestamp rewritten" in result.report()


async def test_an_unaccounted_booking_is_caught(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    """A source booking the plan reached no decision about.

    `sb-missing` is in the source list and in no outcome — neither loaded nor
    dropped. Without a source list carrying such an anchor this assertion cannot
    fail, which is exactly how the equivalent check shipped broken before.
    """
    conn, _plan = loaded
    gapped = plan_of(source_booking_ids=("sb-1", "sb-missing"))

    result = await reconcile_sessions(conn, gapped)

    assert not result.ok
    assert result.unaccounted_bookings == ("sb-missing",)


async def test_an_unaccounted_tracker_is_caught(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    conn, _plan = loaded
    gapped = plan_of(source_tracker_ids=("st-missing",))

    result = await reconcile_sessions(conn, gapped)

    assert not result.ok
    assert result.unaccounted_trackers == ("st-missing",)


async def test_an_absorbed_tracker_counts_as_accounted(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    """The accepting half, and the term easiest to lose.

    A tracker merged into its booking never becomes a row of its own, so a
    reconciliation that only looked at what was loaded would call it missing.
    """
    conn, _plan = loaded
    merged = plan_of(source_tracker_ids=("st-1",), absorbed_trackers=("st-1",))

    result = await reconcile_sessions(conn, merged)

    assert result.unaccounted_trackers == ()


async def test_a_quarantined_tracker_counts_as_accounted(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    conn, _plan = loaded
    held = plan_of(
        source_tracker_ids=("st-9",),
        quarantined=(QuarantinedTracker("st-9", MENTOR, None, "no mentee"),),
    )

    result = await reconcile_sessions(conn, held)

    assert result.unaccounted_trackers == ()


async def test_a_dropped_booking_counts_as_accounted(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    """The bookings identity's second term — the one PR 66's review found
    unreached by any fixture, and which dev data cannot exercise because all
    105 bookings load."""
    conn, _plan = loaded
    dropped = plan_of(
        source_booking_ids=("sb-1", "sb-2"),
        dropped=(DroppedRow("sb-2", "mentor and mentee are the same user"),),
    )

    result = await reconcile_sessions(conn, dropped)

    assert result.unaccounted_bookings == ()


async def test_a_session_with_no_type_is_caught(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    """Zero nulls is the precondition for the later NOT NULL revision.

    The column is nullable today, so nothing else in the gate would notice.
    """
    conn, plan = loaded
    await conn.execute(text("UPDATE sessions SET session_type_id = NULL"))

    result = await reconcile_sessions(conn, plan)

    assert not result.ok
    assert result.sessions_without_type == ("sb-1",)
    assert "no session type" in result.report()


async def test_a_missing_participant_is_caught(
    loaded: tuple[AsyncConnection, SessionPlan],
) -> None:
    """The derived tables carry no anchors, so a count is all there is — but a
    count that nobody compares is not a check."""
    conn, plan = loaded
    await conn.execute(text("DELETE FROM session_participants WHERE role = 'mentee'"))

    result = await reconcile_sessions(conn, plan)

    assert not result.ok
    assert any(check.table == "session_participants" and not check.ok for check in result.checks)

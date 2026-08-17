"""Writing the five session tables, idempotently, in one transaction.

Order is a dependency, not a preference: ``session_types`` before the sessions
that reference them, sessions before the participants and events that hang off
them. Two id maps are read back **from the database** rather than counted from
the plan — a loader that reports what it meant to write cannot report what
landed.

FIVE TABLES, FOUR IDEMPOTENCY KEYS AND ONE DELETE
=================================================
The ETL's recovery plan is a re-run, so every write here has to survive one.

===========================  ===============================================
``sessions``                 ``ON CONFLICT (legacy_bubble_id)``
``session_types``            ``ON CONFLICT (mentor_user_id, name) WHERE …``
``session_type_booking_…``   ``ON CONFLICT (session_type_id)``
``session_participants``     ``ON CONFLICT (session_id, user_id)``
``session_events``           **deleted and rewritten** — it has no key
===========================  ===============================================

``session_events`` carries no ``legacy_bubble_id``: it is derived from columns
of ``SessionBooking`` rather than from a Thing of its own, which settled decision
#27 says anchors on the parent instead. The parent alone is not unique — a
session has two events — so the loader deletes the events **of the sessions in
this plan** and writes them again.

**The table is declared append-only and this deletes from it.** That is not a
contradiction: "revoke UPDATE/DELETE from the application role" is a statement
about the application, and the ETL is not the application. Rewriting a migrated
row's history during a migration is the migration doing its job; doing it
afterwards would not be. The delete is scoped to the plan, so an event belonging
to any other session is untouched.

**The ``ON CONFLICT`` for session types must repeat the index predicate.**
``WHERE deleted_at IS NULL`` is not decoration — omitting it raises
``InvalidColumnReferenceError`` rather than falling back to another index.

WHY ONLY ``sessions`` HOLDS THE TRIGGER OFF
===========================================
``trg_set_updated_at`` is a ``BEFORE UPDATE`` trigger, so it only bites on the
second run — which is the normal path here. But only ``sessions`` carries
timestamps from Bubble. Types, configs, participants and events are **derived**:
they have no source row, so ``now()`` is the honest value and holding the trigger
off for them would be copying M3's shape without its reason.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.transform.sessions import SessionPlan
from app.infra.db.triggers import timestamps_from_source_across

__all__ = ["SessionLoader"]

#: Resolves a Bubble id to a `users.id`, raising rather than returning None.
#: Named so the three private loaders take a function rather than `object` and an
#: `assert callable`, which is a type hole wearing a runtime check.
UserResolver = Callable[[str, str], UUID]

#: Only this one carries Bubble's timestamps. See the module docstring.
STAMPED_TABLES = ("sessions",)

# `DO UPDATE` rather than `DO NOTHING`, because `RETURNING id` yields no row on a
# conflict that did nothing — and the id is what the next table needs.
UPSERT_SESSION_TYPE = """
INSERT INTO session_types (mentor_user_id, name)
VALUES (:mentor_user_id, :name)
ON CONFLICT (mentor_user_id, name) WHERE deleted_at IS NULL
DO UPDATE SET name = EXCLUDED.name
RETURNING id
"""

# `meeting_venue` and `requires_booking_confirmation` are **passed in** now.
#
# They used to be selected off `mentor_profiles`, which worked only because the
# profile load runs first and left them there — two ETL processes coupled through
# columns rather than through a plan. The contract step drops those columns, so
# `SessionTypeRow` carries the values and `transform/profiles.booking_defaults`
# is the single place the legacy fields are read.
#
# The join to `mentor_profiles` stays, and is still inner: `session_types`
# references that table, so an offering whose mentor has no profile row cannot
# exist, and the join asserts it rather than assuming it.
UPSERT_BOOKING_CONFIG = """
INSERT INTO session_type_booking_configs
    (session_type_id, duration_minutes, meeting_venue, requires_booking_confirmation)
VALUES (
    :session_type_id, :duration_minutes,
    :meeting_venue, :requires_booking_confirmation
)
ON CONFLICT (session_type_id) DO UPDATE SET
    duration_minutes              = EXCLUDED.duration_minutes,
    meeting_venue                 = EXCLUDED.meeting_venue,
    requires_booking_confirmation = EXCLUDED.requires_booking_confirmation
"""

# The pointer D88 introduces, set once the offering exists. It cannot be written
# with the profile — `mentor_profiles` and `session_types` reference each other,
# so the row order is profile, offering, pointer.
SET_PRIMARY_OFFERING = """
UPDATE mentor_profiles mp
   SET primary_session_type_id = :session_type_id
 WHERE mp.user_id = :mentor_user_id
   AND mp.primary_session_type_id IS NULL
"""

# Every enum cast is spelled out: the ETL writes raw SQL, so nothing in Pydantic
# or the ORM sits between a transform bug and the column. The enum is what
# refuses a value the vocabulary does not contain.
UPSERT_SESSION = """
INSERT INTO sessions
    (mentor_id, mentee_id, session_type_id, status, starts_at, duration_minutes,
     topic, booking_message, meeting_provider, meeting_url, external_room_id,
     external_calendar_event_id, created_at, updated_at, legacy_bubble_id)
VALUES
    (:mentor_id, :mentee_id, :session_type_id, :status,
     :starts_at, :duration_minutes, :topic, :booking_message,
     :meeting_provider, :meeting_url, :external_room_id,
     :external_calendar_event_id, COALESCE(:created_at, now()),
     COALESCE(:updated_at, now()), :legacy_bubble_id)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    mentor_id                  = EXCLUDED.mentor_id,
    mentee_id                  = EXCLUDED.mentee_id,
    session_type_id            = EXCLUDED.session_type_id,
    status                     = EXCLUDED.status,
    starts_at                  = EXCLUDED.starts_at,
    duration_minutes           = EXCLUDED.duration_minutes,
    topic                      = EXCLUDED.topic,
    booking_message            = EXCLUDED.booking_message,
    meeting_provider           = EXCLUDED.meeting_provider,
    meeting_url                = EXCLUDED.meeting_url,
    external_room_id           = EXCLUDED.external_room_id,
    external_calendar_event_id = EXCLUDED.external_calendar_event_id,
    updated_at                 = EXCLUDED.updated_at
RETURNING id
"""

UPSERT_PARTICIPANT = """
INSERT INTO session_participants
    (session_id, user_id, role, joined_at, attendance_status)
VALUES
    (:session_id, :user_id, :role, :joined_at, :attendance_status)
ON CONFLICT (session_id, user_id) DO UPDATE SET
    role              = EXCLUDED.role,
    joined_at         = EXCLUDED.joined_at,
    attendance_status = EXCLUDED.attendance_status
"""

INSERT_EVENT = """
INSERT INTO session_events
    (session_id, from_status, to_status, actor_id, actor_type, reason_text, created_at)
VALUES
    (:session_id, :from_status,
     :to_status, :actor_id,
     :actor_type, :reason_text, :created_at)
"""

#: Scoped to the sessions this plan wrote. `expanding=True` is the documented
#: way to bind an `IN` list; an empty plan binds nothing and the statement is
#: skipped rather than sent with an empty list, which PostgreSQL rejects.
DELETE_EVENTS = text("DELETE FROM session_events WHERE session_id IN :session_ids").bindparams(
    bindparam("session_ids", expanding=True)
)


class SessionLoader:
    """All five tables, in dependency order, inside the caller's transaction.

    **Returns nothing.** ``AvailabilityLoader`` returns a count object that
    nothing reads, and repeating that here would add a second unused type. What
    landed is read back from the tables by ``reconcile_sessions``; a count
    handed over by the writer is the writer grading its own homework.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def load(self, *, users: dict[str, UUID], plan: SessionPlan) -> None:
        def user_id(bubble_id: str, what: str) -> UUID:
            resolved = users.get(bubble_id)
            if resolved is None:
                # The transform already refuses a session it cannot attribute,
                # so reaching this means the transform and the users table
                # disagree about who exists. Raising is the point: skipping
                # would drop a session with nothing to show for it.
                raise LookupError(f"{what} references unknown user {bubble_id}")
            return resolved

        async with timestamps_from_source_across(self._connection, STAMPED_TABLES):
            type_ids = await self._load_session_types(plan, user_id)
            session_ids = await self._load_sessions(plan, type_ids, user_id)
            await self._load_participants(plan, session_ids, user_id)
            await self._load_events(plan, session_ids, users)

    async def _load_session_types(
        self, plan: SessionPlan, user_id: UserResolver
    ) -> dict[str, UUID]:
        type_ids: dict[str, UUID] = {}
        for row in plan.session_types:
            result = await self._connection.execute(
                text(UPSERT_SESSION_TYPE),
                {
                    "mentor_user_id": user_id(row.mentor_bubble_id, "session type"),
                    "name": row.name,
                },
            )
            type_id = result.scalar_one()
            type_ids[row.mentor_bubble_id] = type_id
            await self._connection.execute(
                text(UPSERT_BOOKING_CONFIG),
                {
                    "session_type_id": type_id,
                    "duration_minutes": row.duration_minutes,
                    "meeting_venue": row.meeting_venue.value,
                    "requires_booking_confirmation": row.requires_booking_confirmation,
                },
            )
            # `IS NULL` in the statement rather than a check here: a re-run must
            # not move a primary somebody has since chosen deliberately, and the
            # loader is re-run as the recovery plan (D40).
            await self._connection.execute(
                text(SET_PRIMARY_OFFERING),
                {
                    "session_type_id": type_id,
                    "mentor_user_id": user_id(row.mentor_bubble_id, "session type"),
                },
            )
        return type_ids

    async def _load_sessions(
        self,
        plan: SessionPlan,
        type_ids: dict[str, UUID],
        user_id: UserResolver,
    ) -> dict[str, UUID]:
        session_ids: dict[str, UUID] = {}
        for row in plan.sessions:
            result = await self._connection.execute(
                text(UPSERT_SESSION),
                {
                    "mentor_id": user_id(row.mentor_bubble_id, "session mentor"),
                    "mentee_id": user_id(row.mentee_bubble_id, "session mentee"),
                    # Never null: every mentor holding a session gets a type, and
                    # `reconcile_sessions` asserts it — that is the precondition
                    # for making the column NOT NULL in a later revision.
                    "session_type_id": type_ids[row.mentor_bubble_id],
                    "status": row.status.value,
                    "starts_at": row.starts_at,
                    "duration_minutes": row.duration_minutes,
                    "topic": row.topic,
                    "booking_message": row.booking_message,
                    "meeting_provider": (
                        row.meeting_provider.value if row.meeting_provider else None
                    ),
                    "meeting_url": row.meeting_url,
                    "external_room_id": row.external_room_id,
                    "external_calendar_event_id": row.external_calendar_event_id,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "legacy_bubble_id": row.legacy_bubble_id,
                },
            )
            session_ids[row.legacy_bubble_id] = result.scalar_one()
        return session_ids

    async def _load_participants(
        self, plan: SessionPlan, session_ids: dict[str, UUID], user_id: UserResolver
    ) -> None:
        for row in plan.participants:
            await self._connection.execute(
                text(UPSERT_PARTICIPANT),
                {
                    "session_id": session_ids[row.session_bubble_id],
                    "user_id": user_id(row.user_bubble_id, "session participant"),
                    "role": row.role.value,
                    "joined_at": row.joined_at,
                    "attendance_status": row.attendance_status.value,
                },
            )

    async def _load_events(
        self, plan: SessionPlan, session_ids: dict[str, UUID], users: dict[str, UUID]
    ) -> None:
        """Delete this plan's events, then write them again.

        The only table here with no key to upsert on. Scoped to the sessions
        this run wrote, so a re-run replaces its own history and touches nobody
        else's — and an empty plan sends no statement at all, because
        PostgreSQL refuses an empty ``IN`` list.
        """
        if not session_ids:
            return
        await self._connection.execute(DELETE_EVENTS, {"session_ids": list(session_ids.values())})
        for row in plan.events:
            await self._connection.execute(
                text(INSERT_EVENT),
                {
                    "session_id": session_ids[row.session_bubble_id],
                    "from_status": row.from_status.value if row.from_status else None,
                    "to_status": row.to_status.value,
                    # Resolved rather than asserted: the transform only writes an
                    # actor it already matched against the user list, so an
                    # unknown one here is a disagreement worth surfacing as None
                    # rather than a crash mid-load.
                    "actor_id": users.get(row.actor_bubble_id or ""),
                    "actor_type": row.actor_type.value,
                    "reason_text": row.reason_text,
                    "created_at": row.created_at,
                },
            )


def session_anchor_map(plan: SessionPlan) -> Sequence[str]:
    """Every session anchor this plan will write, in order.

    Exposed for the script's reporting, so it does not re-derive the list with a
    comprehension of its own — `scripts/` may hold no business rule, and "which
    sessions did this run touch" is one.
    """
    return [row.legacy_bubble_id for row in plan.sessions]

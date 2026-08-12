"""Response shapes for sessions and their lifecycle history.

**Read-only in this phase.** Booking, confirming and cancelling are write paths
that need the cancellation policy, which project conventions record as
deliberately undecided — so publishing a write contract now would encode a rule
nobody has taken.

``starts_at`` goes out as a UTC instant and is never rendered into a local
string. The browser knows the viewer's zone; the server does not, and a session
between Lagos and Toronto has no single correct local time. That is the same
rule the availability schemas follow, and for the same reason.

**Both parties see the message bodies.** ``booking_message`` is what the mentee
wrote to the mentor and ``reason_text`` is why a session ended — each is
addressed to the other party by construction. An admin sees them too: they
already read profiles and availability, and a grant is a grant.

That does **not** contradict the transform's rule that ``report()`` prints no
message body. A report is pasted into terminals and tickets with no access
control; this is an authenticated endpoint behind a scoped query. Treating them
as one problem would be applying a rule past its reason.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from app.domain.enums import (
    ActorType,
    MeetingProvider,
    SessionReasonCode,
    SessionStatus,
)


class SessionRead(BaseModel):
    """One session, as either party sees it."""

    id: str
    #: Both ids are returned so a client can tell which side of the session the
    #: viewer was on. `/users/{id}/sessions` returns every session that user is
    #: a party to, in one list, because a user may be a mentor and a mentee —
    #: dual roles are free by design.
    mentor_id: str
    mentee_id: str
    session_type_id: str | None = Field(
        default=None,
        description=(
            "The mentor's offering this was booked against. Null only on rows "
            "predating the migration that gave every mentor a session type."
        ),
    )
    status: SessionStatus = Field(
        description=(
            "The lifecycle state. **Not derived from attendance** — a session is "
            "`pending_mentor_approval` at creation and `cancelled` if called "
            "off, and neither has attendance to derive from."
        )
    )
    starts_at: dt.datetime = Field(
        description="UTC instant. Render in the viewer's zone client-side."
    )
    duration_minutes: int
    topic: str | None = None
    booking_message: str | None = Field(
        default=None,
        description="What the mentee wrote when booking. Visible to both parties.",
    )
    meeting_provider: MeetingProvider | None = None
    meeting_url: str | None = Field(
        default=None,
        description=(
            "Where the session happens. Generated per session — a static "
            "personal room means back-to-back sessions share it and an early "
            "joiner walks into the previous one."
        ),
    )
    created_at: dt.datetime = Field(description="When the session was booked.")

    @classmethod
    def from_row(cls, row: dict[str, object]) -> SessionRead:
        return cls(
            id=str(row["id"]),
            mentor_id=str(row["mentor_id"]),
            mentee_id=str(row["mentee_id"]),
            session_type_id=str(row["session_type_id"]) if row["session_type_id"] else None,
            status=SessionStatus(str(row["status"])),
            starts_at=row["starts_at"],  # type: ignore[arg-type]
            duration_minutes=int(str(row["duration_minutes"])),
            topic=str(row["topic"]) if row["topic"] else None,
            booking_message=str(row["booking_message"]) if row["booking_message"] else None,
            meeting_provider=(
                MeetingProvider(str(row["meeting_provider"])) if row["meeting_provider"] else None
            ),
            meeting_url=str(row["meeting_url"]) if row["meeting_url"] else None,
            created_at=row["created_at"],  # type: ignore[arg-type]
        )


class SessionEventRead(BaseModel):
    """One transition in a session's history.

    Append-only and immutable: a row states what happened at a moment, and a
    fact that can be edited is not a log.
    """

    id: str
    from_status: SessionStatus | None = Field(
        default=None,
        description=(
            "The state before this transition. Null on the creation event, and "
            "on a migrated event whose prior state the legacy data cannot say."
        ),
    )
    to_status: SessionStatus
    actor_id: str | None = Field(
        default=None,
        description=(
            "Who caused it. **Null means no person did** — an expiry or "
            "no-show sweep — or that a migrated row records an action legacy "
            "did not attribute. Read it with `actor_type`."
        ),
    )
    actor_type: ActorType
    reason_code: SessionReasonCode | None = Field(
        default=None,
        description=(
            "The coded reason, which policy runs on. Null on every migrated "
            "event: legacy held only free text."
        ),
    )
    reason_text: str | None = Field(
        default=None, description="What the person wrote. Visible to both parties."
    )
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: dict[str, object]) -> SessionEventRead:
        return cls(
            id=str(row["id"]),
            from_status=SessionStatus(str(row["from_status"])) if row["from_status"] else None,
            to_status=SessionStatus(str(row["to_status"])),
            actor_id=str(row["actor_id"]) if row["actor_id"] else None,
            actor_type=ActorType(str(row["actor_type"])),
            reason_code=(
                SessionReasonCode(str(row["reason_code"])) if row["reason_code"] else None
            ),
            reason_text=str(row["reason_text"]) if row["reason_text"] else None,
            created_at=row["created_at"],  # type: ignore[arg-type]
        )

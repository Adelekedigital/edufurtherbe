"""Sessions and their lifecycle history, and the one write there is.

**Booking has landed; confirming and cancelling have not.** Those need the
cancellation policy, which project conventions still record as deliberately
undecided, and publishing a write contract for them now would encode a rule
nobody has taken. Creating a session needs no such rule: what may be booked is
already decided by the slot grid.

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
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.domain.enums import (
    ActorType,
    MeetingProvider,
    SessionReasonCode,
    SessionStatus,
)


class PartyRead(BaseModel):
    """One of the two people in a session, as the other one sees them.

    **Every name here is nullable, and that is the data rather than caution.**
    `users.first_name` and `last_name` are both nullable columns, and the M2
    transform maps them straight from optional Bubble fields — `record.get("First
    Name")` — so a migrated user who never filled one in has `NULL`. A party with
    no name at all is a real state, and the nulls go out as nulls: substituting
    "Unknown" here would be a display decision made in the wrong layer, in one
    language, that no client could change.

    `avatar_url` is null for two independent reasons — the column is nullable,
    and the whole `user_profiles` row may not exist. Both arrive as the same
    null, which is why the join is outer.

    **No email, no slug, no `last_active_at`.** The parties are meeting; that
    does not make the rest of each other's account their business.
    """

    id: str
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None


class SessionRead(BaseModel):
    """One session, as either party sees it."""

    id: str
    #: Both ids are returned so a client can tell which side of the session the
    #: viewer was on. `/users/{id}/sessions` returns every session that user is
    #: a party to, in one list, because a user may be a mentor and a mentee —
    #: dual roles are free by design.
    mentor_id: str
    mentee_id: str
    #: The same two people again, named. The bare ids stay because removing them
    #: would break every client reading them today, and because a client that
    #: only needs "which side was I on" should not have to reach into an object.
    mentor: PartyRead
    mentee: PartyRead
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
            mentor=_party(row, "mentor"),
            mentee=_party(row, "mentee"),
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


def _party(row: dict[str, object], side: str) -> PartyRead:
    """Assemble one side from the flat row the store returns.

    One function rather than the same four lines twice: a mentor and a mentee
    differ only by prefix, and two copies is where the mentee quietly stops
    getting the avatar somebody added to the mentor.
    """
    return PartyRead(
        id=str(row[f"{side}_id"]),
        first_name=_text(row.get(f"{side}_first_name")),
        last_name=_text(row.get(f"{side}_last_name")),
        avatar_url=_text(row.get(f"{side}_avatar_url")),
    )


def _text(value: object) -> str | None:
    return str(value) if value is not None else None


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


class SessionBookingWrite(BaseModel):
    """What a mentee sends to book an hour.

    **No `mentor_id`, and no `duration_minutes`.** Both are properties of the
    offering, and accepting either would let a client send one that disagrees
    with it — a request with two answers and no rule for which wins. The mentor
    is derived from `session_type_id`, and the duration is snapshotted from the
    booking config at the moment of booking (settled decision #10's reasoning,
    applied to time rather than money).

    **No `status`.** Whether a booking is confirmed or waits for the mentor is
    the mentor's setting, not the mentee's request.
    """

    session_type_id: UUID = Field(
        description="Which of the mentor's offerings to book. The mentor follows from it."
    )
    starts_at: AwareDatetime = Field(
        description=(
            "**A UTC instant, and it must be one `/slots` currently offers** — "
            "exactly, to the second. Not a local time and not a date: the "
            "mentee and mentor are routinely in different zones, and a naive "
            "value has no single correct reading. A timezone-less string is "
            "refused rather than guessed at.\n\n"
            "Anything the grid does not offer is a `422`, whatever the reason: "
            "inside the notice window, outside the mentor's hours, on a blocked "
            "date, or already taken. The client's response to all four is the "
            "same — re-read `/slots`."
        )
    )
    topic: str | None = Field(default=None, max_length=200)
    booking_message: str | None = Field(
        default=None,
        max_length=2000,
        description="A note to the mentor. Visible to both parties, like every other message here.",
    )

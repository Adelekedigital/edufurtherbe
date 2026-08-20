"""Sessions, their lifecycle history, and the writes that move them along.

**Booking and the four transitions have landed.** What has not is the *refund*
policy — which is a different thing from the cancellation rule and is what
project conventions record as undecided. The transitions can ship without it
because `session_events.reason_code` is the input that policy will read: the
codes are captured now and nothing prices them yet.

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

from app.domain.attendance import join_window
from app.domain.enums import (
    ActorType,
    AttendanceStatus,
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
    joined_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When this party marked themselves present, or `null` if they have "
            "not. **The first arrival, not the latest** — pressing Join again "
            "after a dropped call does not move it.\n\n"
            "`null` while a session is upcoming is the ordinary case, not a "
            "signal."
        ),
    )
    attendance_status: AttendanceStatus = Field(
        default=AttendanceStatus.PENDING,
        description=(
            "**`pending` means *we do not know yet*, not absent.** It is the "
            "state of every party until the join window shuts, and counting it "
            "as absence would report everybody with a session next week as "
            "unreliable.\n\n"
            "A party with no attendance record at all reports `pending` too — "
            "two of the migrated bookings have no participant row, and a "
            "missing row is not an arrival."
        ),
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
    respond_by: dt.datetime | None = Field(
        default=None,
        description=(
            "When this request stops waiting and becomes `expired`, shown to "
            "users as **Unconfirmed**. Six hours before the session.\n\n"
            "**Null on an offering that auto-confirms**, where nothing is "
            "awaiting an answer — not merely unset. A `confirmed` session never "
            "has one.\n\n"
            "Measured backwards from `starts_at`, so the guarantee is to the "
            "*mentee*: you learn the answer before the session, early enough "
            "for it to be useful. It stays on the row after the mentor answers, "
            "as a record of how long they actually had."
        ),
    )
    join_opens_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When either party may first mark themselves present — five "
            "minutes before the start.\n\n"
            "**Sent rather than left to the client to compute**, because the "
            "offsets are a product rule that will become a mentor preference. "
            "A client hardcoding five and fifteen drifts from us the day that "
            "lands, and drifts silently."
        ),
    )
    join_closes_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When the window shuts — fifteen minutes after the start. Joining "
            "after it is refused, and the session's outcome is decided from "
            "this instant.\n\n"
            "**This is the instant a waiting participant needs**, and the "
            'reason it is here: *"your mentor can still join until 15:15"* is '
            'correct, where *"wait up to fifteen minutes"* is wrong for '
            "somebody who arrived at 15:14."
        ),
    )
    created_at: dt.datetime = Field(description="When the session was booked.")
    mentee_attendance_rate: int | None = Field(
        default=None,
        description=(
            "How often this mentee has turned up, as a whole-number percentage "
            "of the sessions they booked that have finished.\n\n"
            "**`null` means no data, and a client must render it as *New "
            "mentee* rather than as `0%`.** Zero says *never shows up*; null "
            "says *we do not know yet*, and every mentee's first booking is "
            "null. The API does not send the words: substituting them here "
            "would be a display decision made in the wrong layer, in one "
            "language, that no client could change — the same reason a party "
            "with no name comes back with nulls rather than *Unknown*.\n\n"
            "Counted over **finished** sessions only. A cancelled one is not a "
            "missed one, and a confirmed one next week has not happened; "
            "either in the denominator would report an absence that never "
            "occurred.\n\n"
            "**There is no matching mentor rate here.** The mentor's is on "
            "their public profile, and it counts sessions they *hosted* — a "
            "different population from the same person's mentee record, which "
            "is why the two are never pooled."
        ),
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> SessionRead:
        # Derived here rather than stored, because it is `starts_at` plus two
        # constants and a stored copy would be a second definition to drift.
        opens, closes = join_window(row["starts_at"])  # type: ignore[arg-type]
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
            respond_by=row.get("respond_by"),  # type: ignore[arg-type]
            join_opens_at=opens,
            join_closes_at=closes,
            created_at=row["created_at"],  # type: ignore[arg-type]
            mentee_attendance_rate=(
                int(str(row["mentee_attendance_rate"]))
                if row.get("mentee_attendance_rate") is not None
                else None
            ),
        )


def _party(row: dict[str, object], side: str) -> PartyRead:
    """Assemble one side from the flat row the store returns.

    One function rather than the same four lines twice: a mentor and a mentee
    differ only by prefix, and two copies is where the mentee quietly stops
    getting the avatar somebody added to the mentor.
    """
    status = row.get(f"{side}_attendance_status")
    return PartyRead(
        id=str(row[f"{side}_id"]),
        first_name=_text(row.get(f"{side}_first_name")),
        last_name=_text(row.get(f"{side}_last_name")),
        avatar_url=_text(row.get(f"{side}_avatar_url")),
        joined_at=row.get(f"{side}_joined_at"),  # type: ignore[arg-type]
        # A missing participant row arrives as `None` and becomes `pending`,
        # which is the same answer as an unsettled row and the right one: both
        # mean *we do not know*, and only a settled row can say otherwise.
        attendance_status=(AttendanceStatus(str(status)) if status else AttendanceStatus.PENDING),
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


class SessionTransitionWrite(BaseModel):
    """Why a session was declined, withdrawn or cancelled. Both fields optional.

    **The two are not one field**, per package D6 and `SessionReasonCode`'s own
    docstring: the text is what a person wrote and the code is what policy runs
    on. A free-text reason cannot answer "what share of mentor-side
    cancellations were scheduling conflicts" without somebody reading two
    hundred rows.

    **Which codes you may send depends on which side of the session you are
    on**, and a code you may not give is a `422` rather than a silently dropped
    field. That is authorization rather than tidiness: the codes drive refund
    policy, so a mentee free to send the mentor's code is a mentee who can claim
    a refund by choosing a value. The permitted sets are not published per-role
    here because they are the *domain's* table, not the schema's — sending one
    you may not give tells you so by name.

    **Accepting takes no body at all.** Agreeing explains itself, and a reason
    field on it would be one more thing for a client to send and for policy to
    have to ignore.
    """

    reason_code: SessionReasonCode | None = Field(
        default=None,
        description=(
            "The coded reason, which policy runs on. Optional — a required one "
            "turns a clear-cut decision into a form to argue with, and every "
            "migrated event carries none because legacy held only free text."
        ),
    )
    reason_text: str | None = Field(
        default=None,
        max_length=2000,
        description="What you want the other party to read. Visible to both of you.",
    )


class SessionCancellationWrite(SessionTransitionWrite):
    """Cancelling, which asks the mentor one extra thing.

    **Its own model rather than a field on the shared one.** All four
    transitions bind `SessionTransitionWrite`, and `accept` deliberately takes
    no body at all — putting `release_slot` there would give agreeing a field
    to send that policy then has to ignore, which that docstring names as the
    thing to avoid. Only cancelling asks, so only cancelling carries it.
    """

    release_slot: bool = Field(
        default=True,
        description=(
            "**Mentors only — a mentee's cancellation always frees the hour.** "
            "Whether you are still free at this time.\n\n"
            "`true` puts the hour back on your grid, which is the default "
            "because the two ways of being wrong are not equally visible: an "
            "hour offered when you are busy shows up as a booking you can "
            "decline, where an hour withheld when you are free shows up as "
            "nothing at all.\n\n"
            "`false` records an **availability exception** on your calendar for "
            "that time — a normal one, which you can see and remove alongside "
            "any other, and which applies to every offering rather than this "
            "one. It is not a hold: it says you are unavailable, not that the "
            "time is reserved for somebody."
        ),
    )

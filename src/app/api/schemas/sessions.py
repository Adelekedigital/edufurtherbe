"""Sessions, their lifecycle history, and the writes that move them along.

**Booking and the four transitions have landed**, and so has the refund rule
(decision 229, `domain/refunds.py`), which reads who acted and when — never
`session_events.reason_code`, which is captured for reporting.

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
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.api.schemas.common import AvatarFocusRead, Normalised, SessionTypeRefRead
from app.domain.attendance import DOOR_STATUSES, door_window, join_window
from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    MeetingProvider,
    SessionReasonCode,
    SessionStatus,
)
from app.domain.intake import MAX_ANSWER_LENGTH, MAX_OPTIONS, MAX_QUESTIONS


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
    #: The person deleted their account (#93, amended 2026-09-29). Their place in
    #: the session stays; their name, avatar and focus are null. Render it as a
    #: deleted account rather than as a missing name, which migrated rows have too.
    deleted: bool = False
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None
    #: Where to centre `avatar_url`; `null` means the client's default crop.
    avatar_focus: AvatarFocusRead | None = None
    timezone: str | None = Field(
        default=None,
        description=(
            "This party's IANA time zone, for showing the other side what the "
            "hour is for them. `null` when they deleted their account."
        ),
    )
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


class AnswerPreviewFirstRead(BaseModel):
    """The first answered question, as one line."""

    question_text: str = Field(description="The question as it reads now.")
    text: str = Field(
        description=(
            "The answer as plain text: the written answer, the chosen options "
            "joined with `, `, or a file's name. May be long; clamp it client-side."
        )
    )


class AnswerPreviewRead(BaseModel):
    """Enough of the intake answers to show on a row without a second request."""

    count: int = Field(
        ge=1,
        description="How many questions were answered, the length of `GET /sessions/{id}/answers`.",
    )
    first: AnswerPreviewFirstRead = Field(
        description="The first answered question in the form's order, the first entry of that list."
    )


class SuggestionRead(BaseModel):
    """Another time the mentor offered when ending this session (#339)."""

    id: str
    starts_at: dt.datetime = Field(description="The offered time. UTC instant.")
    duration_minutes: int
    held_until: dt.datetime = Field(
        description="Until when the time is kept for the mentee alone — two hours from the offer."
    )
    status: Literal["active", "booked", "expired"] = Field(
        description=(
            "`active` while held and unbooked; `booked` once the mentee booked "
            "it; `expired` once the hold lapsed unbooked. An expired time may "
            "still be free — re-read `/slots` — but is no longer held."
        )
    )
    booked_session_id: str | None = Field(
        default=None, description="The session it became, once `booked`."
    )


class JoinRead(BaseModel):
    """Your arrival, recorded, and where to go (#379 follow-up).

    Declared for the reason `DoorRead` is: an untyped dict published the
    response as an arbitrary object, and a generated client got neither field.
    """

    joined: bool = Field(description="Always `true`: a refused arrival is a `409`, never `false`.")
    meeting_url: str | None = Field(
        description=(
            "Where to go. For a Daily session it carries a token minted for you, "
            "expiring when the session ends, and is never stored; for any other "
            "venue it is the session's own address.\n\n"
            "**`null` on a success** means your arrival is recorded but the "
            "venue could not be reached or none is configured — different from "
            "being refused, and worth showing as such."
        )
    )


class DoorRead(BaseModel):
    """Your way into a running session's room (#379).

    **A declared schema rather than a dict**, which Codex caught: returning
    `dict[str, object]` published the response as an arbitrary object, so a
    client generated from the spec got no `meeting_url` property at all and had
    to cast — the opposite of what publishing the contract is for.

    Deliberately carries no `joined`: this endpoint records nothing, and a field
    a client could read an arrival into would undo the reason it exists apart
    from `/join`.
    """

    meeting_url: str | None = Field(
        description=(
            "Where to go. For a Daily session it carries a token minted for you, "
            "expiring when the session ends, and is never stored; for any other "
            "venue it is the session's own address.\n\n"
            "**`null` on a success** means the venue could not be reached or "
            "none is configured — different from being refused, and worth "
            "showing as such."
        )
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
    #: The same offering, named — the session's heading. `null` exactly when
    #: `session_type_id` is.
    session_type: SessionTypeRefRead | None = None
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
            "When either party may first mark themselves present: ten minutes "
            "before the start unless the platform is configured otherwise, and "
            "never more than ten, because cancelling stays open until then.\n\n"
            "**Sent rather than left to the client to compute**, because the "
            "lead is a setting. A client hardcoding any number drifts from us "
            "the day it changes, and drifts silently."
        ),
    )
    join_closes_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When the window shuts — fifteen minutes after the start, or the "
            "session's end if it is shorter than that. Joining after it is "
            "refused, and the session's outcome is decided from this "
            "instant.\n\n"
            "**This is the instant a waiting participant needs**, and the "
            'reason it is here: *"your mentor can still join until 15:15"* is '
            'correct, where *"wait up to fifteen minutes"* is wrong for '
            "somebody who arrived at 15:14."
        ),
    )
    door_closes_at: dt.datetime | None = Field(
        default=None,
        description=(
            "Until when `POST /sessions/{id}/door` hands you a way back into the "
            "room — the session's end. It opens at `join_opens_at`.\n\n"
            "**It ends with the session, because the room does.** Arriving "
            "stops fifteen minutes in, because that is when the outcome is "
            "decided; getting back in after a dropped call does not, because "
            "the session is still running. Show Rejoin until this instant.\n\n"
            "**Never earlier than `join_closes_at`.** For a session longer than "
            "fifteen minutes it is later — that stretch is what the door is "
            "for. For a shorter one the two are equal: arrivals stop when the "
            "session ends, as the room does.\n\n"
            "**`null` when the session has no door at all** — never agreed to, "
            "or called off. It is *not* null once the session settles as "
            "`completed` or `no_show`, so do not key Rejoin off `status`: the "
            "outcome is decided while the room is still open.\n\n"
            "Published so you never compute it. A client adding the duration to "
            "`starts_at` itself drifts from us the day the rule changes."
        ),
    )
    created_at: dt.datetime = Field(description="When the session was booked.")
    suggestion: SuggestionRead | None = Field(
        default=None,
        description=(
            "Another time the mentor offered when they declined or cancelled "
            "this session. `null` when none was."
        ),
    )
    answers_preview: AnswerPreviewRead | None = Field(
        default=None,
        description=(
            "What the mentee answered on the offering's form, in brief: how many "
            "answers and the first. `null` when there are none, including every "
            "migrated booking. The full list, files included, is "
            "`GET /sessions/{id}/answers`, read by the same people as this session."
        ),
    )
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
    def from_row(cls, row: dict[str, object], *, opens_before: dt.timedelta) -> SessionRead:
        # Derived here rather than stored, because it is `starts_at` plus two
        # constants and a stored copy would be a second definition to drift.
        starts_at = cast(dt.datetime, row["starts_at"])
        length = int(str(row["duration_minutes"]))
        opens, closes = join_window(starts_at, length, opens_before=opens_before)
        _, door_closes = door_window(starts_at, length, opens_before=opens_before)
        # Null rather than a time when there is no door, reusing the one set that
        # says which sessions have one — so this field and the endpoint cannot
        # disagree about whether a cancelled session can be entered.
        has_door = SessionStatus(str(row["status"])) in DOOR_STATUSES
        return cls(
            id=str(row["id"]),
            mentor_id=str(row["mentor_id"]),
            mentee_id=str(row["mentee_id"]),
            mentor=_party(row, "mentor"),
            mentee=_party(row, "mentee"),
            session_type_id=str(row["session_type_id"]) if row["session_type_id"] else None,
            session_type=SessionTypeRefRead.of(
                row["session_type_id"], row.get("session_type_name")
            ),
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
            door_closes_at=door_closes if has_door else None,
            created_at=row["created_at"],  # type: ignore[arg-type]
            suggestion=_suggestion(row),
            answers_preview=_answers_preview(row),
            mentee_attendance_rate=(
                int(str(row["mentee_attendance_rate"]))
                if row.get("mentee_attendance_rate") is not None
                else None
            ),
        )


def _answers_preview(row: dict[str, object]) -> AnswerPreviewRead | None:
    preview = row.get("answers_preview")
    return AnswerPreviewRead.model_validate(preview) if preview is not None else None


def _suggestion(row: dict[str, object]) -> SuggestionRead | None:
    if row.get("suggestion_id") is None:
        return None
    booked = row.get("suggestion_booked_session_id")
    return SuggestionRead(
        id=str(row["suggestion_id"]),
        starts_at=row["suggestion_starts_at"],  # type: ignore[arg-type]
        duration_minutes=int(str(row["suggestion_duration_minutes"])),
        held_until=row["suggestion_held_until"],  # type: ignore[arg-type]
        status=str(row["suggestion_status"]),  # type: ignore[arg-type]
        booked_session_id=str(booked) if booked is not None else None,
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
        deleted=bool(row.get(f"{side}_deleted")),
        first_name=_text(row.get(f"{side}_first_name")),
        last_name=_text(row.get(f"{side}_last_name")),
        avatar_url=_text(row.get(f"{side}_avatar_url")),
        avatar_focus=AvatarFocusRead.of(
            row.get(f"{side}_avatar_focus_x"), row.get(f"{side}_avatar_focus_y")
        ),
        timezone=_text(row.get(f"{side}_timezone")),
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


class AnswerWrite(Normalised):
    """One answer to one intake question: exactly one of `text`, `option_ids`
    or `file_id`, whichever the question's type takes."""

    question_id: UUID
    text: str | None = Field(default=None, max_length=MAX_ANSWER_LENGTH)
    option_ids: list[UUID] | None = Field(default=None, max_length=MAX_OPTIONS)
    file_id: UUID | None = Field(
        default=None,
        description=(
            "For a `file_upload` question: the `file_id` `POST /me/intake-files` "
            "returned. Must be your own upload, not yet used in a booking; the "
            "booking links it, so it cannot answer a second one."
        ),
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
    answers: list[AnswerWrite] = Field(
        default_factory=list,
        max_length=MAX_QUESTIONS,
        description=(
            "Answers to the offering's intake form (`SessionTypeRead.questions`), "
            "saved with the booking. `text` for a `free_text` question; "
            "`option_ids` for a `multi_choice` one — exactly one unless the "
            "question `allows_multiple`; `file_id` for a `file_upload` one — upload "
            "the file first with `POST /me/intake-files`. Every required question "
            "must be answered, file questions included. Any problem is a `422` "
            "whose `errors` point at the answer (`/answers/2/option_ids`, "
            "`/answers/0/file_id`) or, for a missing required one, at `/answers`."
        ),
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
    field. Each side reports with its own vocabulary — a mentee cannot file a
    cancellation as `mentor_unavailable` — so what the codes count stays true.
    **A code never decides a refund**: that is decision 229, from the actor's
    side and the notice given. The permitted sets are not published per-role
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


#: Another time a mentor offers when declining or cancelling (#339). One type,
#: so the two bodies that carry it cannot describe it differently.
SuggestedStartsAt = Annotated[
    AwareDatetime | None,
    Field(
        description=(
            "**Mentors only.** Another time to offer the mentee instead (#339). "
            "Must be one `/slots` currently offers for this session's offering, "
            "exactly — anything else is a `422` at `/suggested_starts_at`, and "
            "so is a mentee sending it.\n\n"
            "The session still ends as it would have, refunded the same way; the "
            "suggestion is a separate offer. The time is **held for the mentee "
            "for two hours** — hidden from everyone else's slots and refused to "
            "anyone else's booking — and they book it with an ordinary `POST "
            "/sessions` at that time. They are emailed once, with both the news "
            "and the offer, and reminded thirty minutes before the hold lapses. "
            "The offer appears on this session as `suggestion`."
        ),
    ),
]


class SessionDeclineWrite(SessionTransitionWrite):
    """Declining, which may offer another time instead (#339).

    Its own model for the reason cancelling has one: withdrawing binds the
    shared body, and a mentee taking back a request has no time to suggest.
    """

    suggested_starts_at: SuggestedStartsAt = None


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

    suggested_starts_at: SuggestedStartsAt = None

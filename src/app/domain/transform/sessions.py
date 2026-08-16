"""M4 session Things into the rows the session tables expect.

Pure: dictionaries in, dataclasses out. Same contract as ``identity``,
``profiles`` and ``availability``.

**Two source Things become one table.** ``SessionBooking`` and
``SessionTracker`` were a Bubble workaround rather than a domain distinction
(package D3), measured rather than assumed: across the 103 linked pairs in the
dev export the two rows agree on mentor, duration, cancellation flag, room name
and meeting venue **103 times out of 103**. Only ``starts_at`` differs, on two
rows.

That shape is why this module carries **two** accounting identities rather than
one. A tracker merged into its booking is *absorbed*, not *loaded*: count it as
loaded and the totals still balance while a row has quietly vanished.

THE PACKAGE'S FIELD MAPPING IS WRONG ON FOUR POINTS
===================================================
Each measured, each recorded as settled decision #83:

1. ``Meeting venue`` and ``meetinglink`` hold a **URL**, not a provider. They
   feed ``meeting_url``; the provider is derived from the host.
2. ``expiration`` is ``session time + 15 minutes`` on 255 of 266 rows — a
   join-window cutoff, **not** a booking expiry. It becomes no event at all.
3. ``SessionDateTime-UTC`` carries **no** UTC shift despite its name.
   ``Last Joined`` is a system clock and sits within minutes of ``session time``
   on 52/52 and 51/51 rows, with none in the 4-5 hour band. So it is read with
   ``assume=export_timezone`` like every other export timestamp.
4. ``Creator`` reads ``(App admin)`` on 105 of 105 bookings — a workflow, not a
   person — so ``created_by`` is null on every migrated row.

A fifth, found while deciding the event rules: the package says ``session_events``
replaces ``statusApproved-DeclinedDate``. That field belongs to
``Mentor (front search)`` and the package maps it correctly to
``mentor_profiles.approved_at`` elsewhere. **There is no session-approval
timestamp in legacy at all.**

WHAT A MIGRATED EVENT MAY CLAIM
===============================
``Modified Date`` **is** the last state transition, measured: all 92 cancelled
and 1 declined booking were last modified *before* their session; all 10 missed
and 1 completed *after* it, eight of them within half an hour — the sweep firing
just past the join window. The separation is exactly what the semantics predict.

So each session gets at most **two** events: creation at ``Creation Date``, and
the terminal transition at ``Modified Date``. Never three. Four dev rows are
confirmed-then-missed, and their confirmation cannot be placed in time — so the
terminal event carries ``from_status = confirmed`` to record the state, and no
third event is invented to carry a timestamp we do not have.

A ``Pending`` session gets no terminal event. It was modified and never
transitioned.

**A cancelled session is never also missed.** Where ``SessionCancel`` says yes,
the session is cancelled whatever ``sessionStatus`` says — three dev rows say
``Missed`` while carrying a mentor canceller, a reason and the flag, because the
sweep overwrote a decision a human had already made. That inconsistency is a
legacy defect and is not carried forward.

ATTRIBUTION
===========
Bookings attribute from their own links — ``Session Initiator`` and ``🕵Mentor``,
both populated on every row. Orphan trackers have no booking to inherit from and
``Mentee(userdatatype)`` on only 31 of 164, so ``Creator`` is the fallback; where
both are present they agree on 27 of 27. An orphan with neither is quarantined
rather than attributed to a guess.

``CalendarSettings`` is attributed by ``Creator`` alone, which
``transform/availability.py`` already records as the second exception to the
user-side-link rule (settled decision #60). That rule is imported from there
rather than re-derived here.

**This module reads ``CalendarSettings`` rows that ``plan_availability``
refuses to load.** Generation A is quarantined for its *times* — the ambiguity
settled decision #81 describes — and ``meetingDuration-TxT`` is a different
field, populated on all 12 rows of each generation. A mentor whose schedule
cannot be migrated still has a duration, and taking it is not a partial lift of
the quarantine.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.domain.bubble import CREATED_AT, MODIFIED_AT, legacy_anchor, parse_timestamp
from app.domain.enums import (
    ActorType,
    AttendanceStatus,
    MeetingProvider,
    SessionRole,
    SessionStatus,
)
from app.domain.transform.availability import CREATOR_FIELD, DroppedRow
from app.domain.transform.identity import TransformError
from app.domain.transform.profiles import (
    MENTOR_LINK_FIELD,
    booking_defaults,
    mentor_owner_map,
)

# --------------------------------------------------------------------------
# Legacy field names
#
# Three carry emoji and are written as escape sequences, for the reason
# `profiles.py` and `availability.py` both do the same: a literal is unreadable
# in a diff and breaks on any tool opening the file as cp1252, the Windows
# default.
# --------------------------------------------------------------------------

# SessionBooking
BOOKING_MENTOR_FIELD = "\U0001f575Mentor"
BOOKING_MENTEE_FIELD = "Session Initiator"
BOOKING_TRACKER_FIELD = "SessionTracker"
BOOKING_START_FIELD = "SessionDateTime-UTC(Booked date)"
BOOKING_DURATION_FIELD = "Duration"
BOOKING_STATUS_FIELD = "sessionStatus"
BOOKING_CANCEL_FLAG_FIELD = "SessionCancel (Y/N)❌"
BOOKING_CANCELLED_BY_FIELD = "Canceled By"
BOOKING_CANCEL_MESSAGE_FIELD = "Session Cancel/Decline Message"
BOOKING_ACCEPTED_FIELD = "bookingRequestAccepted"
BOOKING_TOPIC_FIELD = "Session topic"
BOOKING_MESSAGE_FIELD = "\U0001f4ac Session booking Message"
BOOKING_VENUE_FIELD = "Meeting venue"
BOOKING_CALENDAR_EVENT_FIELD = "googleCalEventId"

# SessionTracker
TRACKER_SESSION_FIELD = "SessionID"
TRACKER_MENTOR_FIELD = "Mentor(this session)"
TRACKER_MENTEE_FIELD = "Mentee(userdatatype)"
TRACKER_START_FIELD = "session time"
TRACKER_DURATION_FIELD = "Session Duration"
TRACKER_CANCELLED_FIELD = "Canceled"
TRACKER_LINK_FIELD = "meetinglink"
TRACKER_MENTEE_JOINED_FIELD = "Last Joined(mentee)"
TRACKER_MENTOR_JOINED_FIELD = "Last Joined(Mentor)"
TRACKER_MENTEE_STATUS_FIELD = "TrackStatus(mentee)"
TRACKER_MENTOR_STATUS_FIELD = "TrackStatus(Mentor)"

# Shared
ROOM_NAME_FIELD = "google/dailyRoomName"
MEETING_DURATION_FIELD = "meetingDuration-TxT"

# Timestamps are read under the **canonical** names, never the export's own.
#
# A record reaching a transform has been through an adapter, so it carries
# `created_at` and `modified_at` — not `Creation Date`, which is what the raw
# JSON holds. Reading the raw key here returned `None` for every row and skipped
# every event, silently: the first draft of this module did exactly that, and a
# probe against the raw files passed because it exercised a shape the pipeline
# never produces.
#
# This is the sixth source difference (settled decision #37) and the reason
# `CREATED_AT` and `MODIFIED_AT` exist at all — the same trap that once refused
# all 43 records and, in M2, wrote `"updated_at"` by hand in four places.

#: Dropped, each for a recorded reason. The five derivable-from-`starts_at`
#: fields, the duplicated identity columns, and the two PostHog flags which
#: belong to `outbox_events` rather than to a domain table.
#:
#: `expiration` is here and is **not** derivable: it is a join-window cutoff and
#: the package's `-> expired event` reading would invent a lifecycle that never
#: happened (settled decision #83).
#:
#: `datePicked` is the one the package gets wrong in the other direction. It
#: calls it "DERIVED from starts_at"; it is *earlier* than `starts_at` on 15 of
#: 16 rows, so it is a previously chosen slot. It still cannot populate
#: `rescheduled_from_session_id` — that column wants a session id and legacy
#: rescheduled by editing the row in place — so it is reported, not stored.
DROPPED_FIELDS = (
    "datePicked",
    "datePickedText",
    "slotBookedTime",
    "Weekday(number)",
    "expiration",
    "TrackID",
    "Mentor(userdatatype)",
    "trackedSessionPosthog(Y/N)",
    "sessionTrackedPosthog",
)

#: The auto-created session type every migrated mentor receives, so their
#: sessions can carry a `session_type_id` and the column can become NOT NULL
#: afterwards (package D21).
GENERAL_MENTORSHIP = "General Mentorship"

#: Where a mentor has no `CalendarSettings` at all. 45 is the legacy mode on
#: both sides — 12 of 24 calendar rows and 97 of 105 bookings — so it is the
#: value the data points at rather than a round number.
#:
#: **No dev mentor needs it**, which is why the path has a synthetic fixture: a
#: fallback with no instance in the export ships unexercised otherwise.
DEFAULT_DURATION_MINUTES = 45

#: Legacy `sessionStatus` to the lifecycle enum. Five values in the dev export.
#:
#: An **absent** status is dropped with an anchor; a **present but unrecognised**
#: one raises. That asymmetry is `_resolve_timezone`'s rule in `identity.py`, and
#: it matters here because dev holds 105 rows against production's 1,073: a sixth
#: value is likelier than not, and mapping it to a plausible neighbour would
#: produce sessions nobody could find again.
STATUS_MAP: dict[str, SessionStatus] = {
    "canceled": SessionStatus.CANCELLED,
    "cancelled": SessionStatus.CANCELLED,
    "missed": SessionStatus.NO_SHOW,
    "declined": SessionStatus.DECLINED,
    "completed": SessionStatus.COMPLETED,
    "pending": SessionStatus.PENDING_MENTOR_APPROVAL,
}

#: Host to provider. The legacy columns hold URLs, so the provider is derived
#: rather than mapped — `MeetingProvider.ZOOM` has no legacy source and must
#: never be invented here.
PROVIDER_HOSTS: dict[str, MeetingProvider] = {
    "meet.google.com": MeetingProvider.GOOGLE_MEET,
    "edufurther.daily.co": MeetingProvider.DAILY,
}

TRUE_VALUES = {"yes", "true"}

#: The column CHECK. Stated here so the transform refuses a row the loader would
#: abort on — two dev trackers carry `1` and `4`.
MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 480


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRow:
    legacy_bubble_id: str
    mentor_bubble_id: str
    mentee_bubble_id: str
    status: SessionStatus
    starts_at: dt.datetime
    duration_minutes: int
    topic: str | None
    booking_message: str | None
    meeting_provider: MeetingProvider | None
    meeting_url: str | None
    external_room_id: str | None
    external_calendar_event_id: str | None
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
    #: True where this row came from a tracker with no booking. Carried so
    #: reconciliation can tell the two populations apart without re-deriving it.
    from_orphan_tracker: bool = False


@dataclass(frozen=True, slots=True)
class ParticipantRow:
    session_bubble_id: str
    user_bubble_id: str
    role: SessionRole
    joined_at: dt.datetime | None
    attendance_status: AttendanceStatus


@dataclass(frozen=True, slots=True)
class SessionEventRow:
    session_bubble_id: str
    from_status: SessionStatus | None
    to_status: SessionStatus
    actor_bubble_id: str | None
    actor_type: ActorType
    reason_text: str | None
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class SessionTypeRow:
    """A mentor's offering, and the booking config that ships with it.

    ``meeting_venue`` and ``requires_booking_confirmation`` are **per offering**
    (D88) and travel here rather than being read back off ``mentor_profiles``.
    The loader used to select them from that table, which worked only because the
    profile load happens first and left them there; the contract step drops those
    columns, so a value that crossed two ETL processes through the database now
    crosses one transform instead.
    """

    mentor_bubble_id: str
    name: str
    duration_minutes: int
    meeting_venue: MeetingProvider
    requires_booking_confirmation: bool


@dataclass(frozen=True, slots=True)
class QuarantinedTracker:
    """An orphan tracker that cannot become a session, and what it lacks."""

    legacy_bubble_id: str
    mentor_bubble_id: str | None
    starts_at: dt.datetime | None
    reason: str

    def __str__(self) -> str:
        return f"{self.legacy_bubble_id}: {self.reason}"


@dataclass(frozen=True, slots=True)
class Disagreement:
    """Two sources stating the same fact differently. The anchor is a field.

    Same shape and same reason as ``DroppedRow``: reconciliation and the
    operator both need the id, and an id inside a sentence has to be parsed back
    out.
    """

    legacy_bubble_id: str
    detail: str

    def __str__(self) -> str:
        return f"{self.legacy_bubble_id}: {self.detail}"


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """Everything one extract turns into, before any of it is written."""

    sessions: tuple[SessionRow, ...]
    participants: tuple[ParticipantRow, ...]
    events: tuple[SessionEventRow, ...]
    session_types: tuple[SessionTypeRow, ...]

    #: Orphan trackers with no identifiable second party. Reported, never loaded.
    quarantined: tuple[QuarantinedTracker, ...] = ()
    #: Source rows that did not become one. **Accounting, not a diagnostic.**
    dropped: tuple[DroppedRow, ...] = ()
    #: Trackers merged into their booking. They never reach the database, so
    #: reconciliation has to be told or the tracker total will not add up.
    absorbed_trackers: tuple[str, ...] = ()

    #: `Creator` disagreeing with the user-side link. The link wins; this exists
    #: so the disagreement is visible rather than absorbed (settled decision #60).
    creator_mismatches: tuple[Disagreement, ...] = ()
    #: A booking and its tracker stating different start times. Booking wins.
    starts_at_disagreements: tuple[Disagreement, ...] = ()
    #: `SessionCancel` said yes while `sessionStatus` said something else.
    #: Cancellation wins — a cancelled session is never also missed.
    cancellation_overrode_status: tuple[Disagreement, ...] = ()
    #: Cancellations whose only available timestamp post-dates the session,
    #: because the sweep modified the row after the cancel. Right state, wrong
    #: clock, said out loud rather than smoothed over.
    cancel_time_after_session: tuple[Disagreement, ...] = ()
    #: Confirmations we know happened and cannot place in time.
    unplaceable_confirmations: tuple[str, ...] = ()
    #: `google/dailyRoomName` holding a display string rather than a room id.
    junk_room_names: tuple[Disagreement, ...] = ()
    #: Mentors whose `CalendarSettings` rows disagree on duration. Modal wins.
    duration_disagreements: tuple[Disagreement, ...] = ()
    #: Mentors with no `CalendarSettings` at all, given the legacy mode.
    duration_defaulted: tuple[str, ...] = ()
    #: Mentors holding sessions for whom no mentor record was found, whose
    #: offerings therefore take the column defaults rather than their venue
    #: and confirmation setting.
    #:
    #: **This exists because the failure it reports was silent.** Reading the
    #: wrong Bubble Thing — `allmentors` rather than `mentorsearch` — parses
    #: cleanly, matches no anchor, and produces a database where every count
    #: reconciles and every mentor is on `google_meet`. Nothing else notices.
    booking_defaulted: tuple[str, ...] = ()
    #: Sessions carrying a previously chosen slot — evidence of a reschedule
    #: that cannot be linked, because legacy edited the row in place.
    reschedule_evidence: tuple[Disagreement, ...] = ()
    #: A session mentor with no user-side mentor link. A cross-check, not the
    #: attribution path: mentors come from the session rows themselves.
    mentors_without_link: tuple[str, ...] = ()
    #: Live-status windows colliding for one mentor. **The pre-flight**: the
    #: exclusion constraint would abort the load, so these are fixed in Bubble
    #: while it is still writable rather than discovered inside the freeze.
    overlapping_live_windows: tuple[Disagreement, ...] = ()

    #: Every source anchor the transform was handed, carried so reconciliation
    #: can assert the identity rather than assume it.
    source_booking_ids: tuple[str, ...] = ()
    source_tracker_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors

    def accounted_for_bookings(self) -> tuple[str, ...]:
        """Every booking anchor this run reached a decision about.

        A booking is **loaded or dropped** — there is no third outcome, because
        a booking always carries its own mentor and mentee.
        """
        return tuple(
            row.legacy_bubble_id for row in self.sessions if not row.from_orphan_tracker
        ) + tuple(
            row.legacy_bubble_id
            for row in self.dropped
            if row.legacy_bubble_id in self._booking_ids
        )

    def accounted_for_trackers(self) -> tuple[str, ...]:
        """Every tracker anchor this run reached a decision about.

        Four outcomes, and ``absorbed`` is the one that is easy to lose: a
        tracker merged into its booking never becomes a row of its own, so
        counting only what was loaded makes the total short by exactly the
        number of successful merges.
        """
        return (
            tuple(row.legacy_bubble_id for row in self.sessions if row.from_orphan_tracker)
            + tuple(held.legacy_bubble_id for held in self.quarantined)
            + self.absorbed_trackers
            + tuple(
                row.legacy_bubble_id
                for row in self.dropped
                if row.legacy_bubble_id not in self._booking_ids
            )
        )

    @property
    def _booking_ids(self) -> frozenset[str]:
        return frozenset(self.source_booking_ids)

    def report(self) -> str:
        """The operator-facing account of one run.

        Lives in ``domain`` beside the decisions it summarises (settled decision
        #45).

        **No free text is printed.** `booking_message` and the cancellation
        message are the most sensitive content in this phase, and a report is
        pasted into terminals and tickets. Counts and anchors only.
        """
        lines = [
            f"sessions loaded      {len(self.sessions)}"
            f" ({sum(1 for s in self.sessions if s.from_orphan_tracker)} from orphan trackers)",
            f"participants         {len(self.participants)}",
            f"events               {len(self.events)}",
            f"session types        {len(self.session_types)}",
            f"trackers absorbed    {len(self.absorbed_trackers)}",
            f"quarantined          {len(self.quarantined)}",
            f"dropped              {len(self.dropped)}",
        ]

        for label, values in (
            ("dropped", self.dropped),
            ("quarantined orphan trackers", self.quarantined),
            ("cancellation overrode status", self.cancellation_overrode_status),
            ("cancel timestamp post-dates the session", self.cancel_time_after_session),
            ("start-time disagreements (booking wins)", self.starts_at_disagreements),
            ("creator disagrees with the link", self.creator_mismatches),
            ("junk room names", self.junk_room_names),
            ("duration disagreements (modal wins)", self.duration_disagreements),
            ("duration defaulted to the legacy mode", self.duration_defaulted),
            ("venue and confirmation defaulted (no mentor record)", self.booking_defaulted),
            ("reschedule evidence, unlinkable", self.reschedule_evidence),
            ("session mentors with no mentor link", self.mentors_without_link),
            ("confirmations that cannot be placed in time", self.unplaceable_confirmations),
            (
                "OVERLAPPING LIVE WINDOWS - fix in Bubble before the freeze",
                self.overlapping_live_windows,
            ),
            ("errors", self.errors),
        ):
            if values:
                lines.append(f"\n{label} ({len(values)}):")
                lines.extend(f"  {value}" for value in values)

        return "\n".join(lines)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _text(record: dict[str, Any], field_name: str) -> str | None:
    value = str(record.get(field_name) or "").strip()
    return value or None


def _timestamp(
    record: dict[str, Any], field_name: str, *, assume: dt.tzinfo, anchor: str
) -> dt.datetime | None:
    raw = record.get(field_name)
    if not raw:
        return None
    try:
        return parse_timestamp(str(raw), assume=assume)
    except ValueError as exc:
        raise TransformError(anchor, f"{field_name}: {exc}") from exc


def _is_true(value: Any) -> bool:
    """``blank_to_none`` turns ``""`` into ``None``, so this must be falsy-safe.

    ``bookingRequestAccepted`` is blank on 100 of 105 rows; ``== ""`` would
    classify every one of them as present-and-not-yes rather than absent.
    """
    return str(value or "").strip().lower() in TRUE_VALUES


def _duration(raw: Any) -> int | None:
    """Minutes, or ``None`` where the value cannot satisfy the column CHECK."""
    text = str(raw or "").strip()
    if not text.isdigit():
        return None
    minutes = int(text)
    if not MIN_DURATION_MINUTES <= minutes <= MAX_DURATION_MINUTES:
        return None
    return minutes


def meeting_provider(url: str | None) -> MeetingProvider | None:
    """Derived from the host, because the legacy column holds a URL.

    The package's field mapping sends ``Meeting venue`` and ``meetinglink`` to
    ``meeting_provider``; both hold a URL on every populated dev row, and the
    booking and its tracker agree on all 103 linked pairs.

    An unrecognised host is ``CUSTOM`` — the vocabulary's own word for "a link
    the platform did not create". A value that is not a URL at all yields
    ``None``: eight tracker rows hold bare room tokens and two hold profile bio
    text.
    """
    if not url or not url.startswith("http"):
        return None
    host = urlparse(url).netloc.lower()
    if host in PROVIDER_HOSTS:
        return PROVIDER_HOSTS[host]
    return MeetingProvider.CUSTOM


def room_id(raw: Any) -> str | None:
    """A room token, or ``None`` where the column holds a display string.

    ``google/dailyRoomName`` reads the literal ``Google Meet`` on 180 rows. A
    room id has no spaces, which is the cheapest discriminator that does not
    hard-code the one bad value — a second display string would be caught too.
    """
    text = str(raw or "").strip()
    if not text or " " in text or text.startswith("http"):
        return None
    return text


def _attendance(raw: Any) -> AttendanceStatus:
    """``TrackStatus`` is yes/no and agrees with ``Last Joined`` on 267/267 rows.

    ``LEFT_EARLY`` is unreachable from legacy: nothing in Bubble records a
    departure, so ``left_at`` is null on every migrated participant.
    """
    return AttendanceStatus.ATTENDED if _is_true(raw) else AttendanceStatus.NO_SHOW


def session_status(record: dict[str, Any], anchor: str) -> SessionStatus | None:
    """The lifecycle state, with cancellation outranking the status column.

    **A cancelled session is never also missed.** Three dev rows carry
    ``sessionStatus = Missed`` alongside the cancel flag, a mentor canceller and
    a reason — the automated sweep overwrote a decision a human had already
    made. Legacy was inconsistent; this is where that stops.

    Returns ``None`` for an absent status, which the caller drops with an
    anchor. A **present but unrecognised** value raises: at 1,073 production
    rows a sixth value is likelier than not, and mapping it to a plausible
    neighbour would produce sessions nobody could find again.
    """
    if _is_true(record.get(BOOKING_CANCEL_FLAG_FIELD)):
        return SessionStatus.CANCELLED

    raw = _text(record, BOOKING_STATUS_FIELD)
    if raw is None:
        return None
    try:
        return STATUS_MAP[raw.lower()]
    except KeyError as exc:
        raise TransformError(anchor, f"unmapped sessionStatus {raw!r}") from exc


def durations_by_mentor(
    calendar_settings: list[dict[str, Any]],
) -> tuple[dict[str, int], list[Disagreement]]:
    """Each mentor's session-type duration, from ``meetingDuration-TxT``.

    Legacy held duration per *calendar row*, not per mentor, so a mentor with
    several rows can disagree with themselves. No dev mentor does; production
    has 44 mentors over 192 rows, so the rule is stated rather than assumed: the
    **modal** value wins and every disagreement is reported.

    Attribution is ``Creator``, imported from ``availability`` rather than
    re-derived — that is settled decision #60's second exception and it should
    have exactly one definition.

    **Generation A rows count here.** They are quarantined for their *times*
    (settled decision #81); ``meetingDuration-TxT`` is a different field and is
    populated on all 12 rows of each generation.
    """
    seen: dict[str, list[int]] = {}
    for record in calendar_settings:
        owner = record.get(CREATOR_FIELD)
        minutes = _duration(record.get(MEETING_DURATION_FIELD))
        if owner and minutes is not None:
            seen.setdefault(str(owner), []).append(minutes)

    durations: dict[str, int] = {}
    disagreements: list[Disagreement] = []
    for owner, values in sorted(seen.items()):
        counted = Counter(values)
        # `min` on a tie so the same input always gives the same answer; a modal
        # tie decided by dict order would make the load non-deterministic.
        modal = min(counted, key=lambda minutes: (-counted[minutes], minutes))
        durations[owner] = modal
        if len(counted) > 1:
            disagreements.append(
                Disagreement(owner, f"calendar rows disagree {sorted(counted)}; using {modal}")
            )
    return durations, disagreements


def _participants(
    tracker: dict[str, Any], anchor: str, mentor: str, mentee: str, *, assume: dt.tzinfo
) -> list[ParticipantRow]:
    """Two rows from four parallel legacy columns."""
    return [
        ParticipantRow(
            session_bubble_id=anchor,
            user_bubble_id=mentor,
            role=SessionRole.MENTOR,
            joined_at=_timestamp(
                tracker, TRACKER_MENTOR_JOINED_FIELD, assume=assume, anchor=anchor
            ),
            attendance_status=_attendance(tracker.get(TRACKER_MENTOR_STATUS_FIELD)),
        ),
        ParticipantRow(
            session_bubble_id=anchor,
            user_bubble_id=mentee,
            role=SessionRole.MENTEE,
            joined_at=_timestamp(
                tracker, TRACKER_MENTEE_JOINED_FIELD, assume=assume, anchor=anchor
            ),
            attendance_status=_attendance(tracker.get(TRACKER_MENTEE_STATUS_FIELD)),
        ),
    ]


def _terminal_actor(
    status: SessionStatus, cancelled_by: str | None
) -> tuple[str | None, ActorType]:
    """Who caused the terminal transition.

    ``NO_SHOW`` is the automated sweep — measured firing within half an hour of
    the session on eight of ten rows — so it is genuinely ``SYSTEM`` with no
    actor. Everything else was a person: named where ``Canceled By`` holds them,
    and ``USER`` with a null id where it does not, because "a person we cannot
    name" is a different claim from "the system did it".
    """
    if status is SessionStatus.NO_SHOW:
        return None, ActorType.SYSTEM
    return cancelled_by, ActorType.USER


GUARDED_STATUSES = frozenset({SessionStatus.PENDING_MENTOR_APPROVAL, SessionStatus.CONFIRMED})


def overlapping_windows(sessions: list[SessionRow]) -> list[Disagreement]:
    """Live windows colliding for one mentor — the extract-time pre-flight.

    ``sessions_no_mentor_double_booking`` refuses these, so an overlap aborts the
    load. Finding them **at the extract** is what makes them fixable in Bubble
    while it is still writable; finding them at load time means finding them
    inside the freeze window.

    The expected result is zero. Legacy did prevent double-booking, and mostly
    from the frontend (settled decision #84) — which cannot see two people
    clicking in the same second and is skipped by any path avoiding that screen.
    So a non-zero result is a race or a bypass, and is worth understanding rather
    than merely cleaning.
    """
    live = sorted(
        (row for row in sessions if row.status in GUARDED_STATUSES),
        key=lambda row: (row.mentor_bubble_id, row.starts_at),
    )
    found: list[Disagreement] = []
    for index, row in enumerate(live):
        for other in live[index + 1 :]:
            if other.mentor_bubble_id != row.mentor_bubble_id:
                break
            if other.starts_at >= row.starts_at + dt.timedelta(minutes=row.duration_minutes):
                break
            found.append(
                Disagreement(
                    row.legacy_bubble_id,
                    f"overlaps {other.legacy_bubble_id} for mentor {row.mentor_bubble_id}",
                )
            )
    return found


def plan_sessions(
    user_records: list[dict[str, Any]],
    bookings: list[dict[str, Any]],
    trackers: list[dict[str, Any]],
    calendar_settings: list[dict[str, Any]],
    mentor_records: list[dict[str, Any]],
    *,
    export_timezone: dt.tzinfo,
) -> SessionPlan:
    """Every session Thing, merged and classified, before anything is written.

    **Takes the raw records** rather than maps a caller assembled, matching
    ``plan_availability`` and ``plan_profiles``: deciding who counts as a mentor
    and what a mentor's duration is are mapping decisions, and doing them in
    ``scripts/`` would put business rules in the one directory the gate cannot
    see (settled decision #44).

    ``mentor_records`` arrived with D88's contract step. Each offering carries its
    own venue and confirmation setting, and until that step they reached the
    config by being written to ``mentor_profiles`` by the *profile* load and
    selected back out by the *session* load — two processes, coupled through
    columns that no longer exist. The mapping is `profiles.booking_defaults`, so
    both transforms read the legacy fields exactly one way.
    """
    known_users = {legacy_anchor(record) for record in user_records}
    mentor_linked = {
        legacy_anchor(record) for record in user_records if record.get(MENTOR_LINK_FIELD)
    }
    # Keyed on the mentor's *user* anchor, because that is what a session names.
    booking_by_mentor: dict[str, tuple[MeetingProvider, bool]] = {}
    # A mentor record nobody points at is skipped rather than raising: the profile
    # load already reports those as unattached, and reporting the same orphan from
    # a second phase would double-count it.
    mentor_owner = mentor_owner_map(user_records)
    for record in mentor_records:
        anchor = legacy_anchor(record)
        user_anchor = mentor_owner.get(anchor)
        if user_anchor is None:
            continue
        booking_by_mentor[user_anchor] = booking_defaults(record, bubble_id=anchor)
    durations, duration_disagreements = durations_by_mentor(calendar_settings)
    trackers_by_anchor = {legacy_anchor(record): record for record in trackers}

    sessions: list[SessionRow] = []
    participants: list[ParticipantRow] = []
    events: list[SessionEventRow] = []
    quarantined: list[QuarantinedTracker] = []
    dropped: list[DroppedRow] = []
    absorbed: list[str] = []
    creator_mismatches: list[Disagreement] = []
    starts_disagreements: list[Disagreement] = []
    cancel_overrode: list[Disagreement] = []
    cancel_late: list[Disagreement] = []
    unplaceable: list[str] = []
    junk_rooms: list[Disagreement] = []
    reschedules: list[Disagreement] = []
    mentors_without_link: list[str] = []
    errors: list[str] = []

    source_booking_ids = [legacy_anchor(record) for record in bookings]
    source_tracker_ids = [legacy_anchor(record) for record in trackers]

    # ---------------------------------------------------------------- bookings
    for record in bookings:
        anchor = legacy_anchor(record)
        try:
            status = session_status(record, anchor)
            starts_at = _timestamp(
                record, BOOKING_START_FIELD, assume=export_timezone, anchor=anchor
            )
            created_at = _timestamp(record, CREATED_AT, assume=export_timezone, anchor=anchor)
            modified_at = _timestamp(record, MODIFIED_AT, assume=export_timezone, anchor=anchor)
        except TransformError as exc:
            errors.append(str(exc))
            continue

        mentor = _text(record, BOOKING_MENTOR_FIELD)
        mentee = _text(record, BOOKING_MENTEE_FIELD)
        minutes = _duration(record.get(BOOKING_DURATION_FIELD))

        if status is None:
            dropped.append(DroppedRow(anchor, "no sessionStatus"))
            continue
        if not mentor or not mentee:
            dropped.append(DroppedRow(anchor, "booking has no mentor or no initiator"))
            continue
        if mentor == mentee:
            dropped.append(DroppedRow(anchor, "mentor and mentee are the same user"))
            continue
        if starts_at is None:
            dropped.append(DroppedRow(anchor, "no session date"))
            continue
        if minutes is None:
            dropped.append(
                DroppedRow(
                    anchor, f"duration {record.get(BOOKING_DURATION_FIELD)!r} fails the CHECK"
                )
            )
            continue

        if mentor not in mentor_linked:
            mentors_without_link.append(mentor)
        creator = _text(record, CREATOR_FIELD)
        if creator and creator in known_users and creator != mentee:
            creator_mismatches.append(
                Disagreement(anchor, f"Creator {creator} is not the initiator")
            )
        if record.get("datePicked"):
            reschedules.append(Disagreement(anchor, "carries a previously chosen slot"))

        declared = _text(record, BOOKING_STATUS_FIELD)
        if status is SessionStatus.CANCELLED and declared and declared.lower() != "canceled":
            cancel_overrode.append(
                Disagreement(anchor, f"sessionStatus said {declared!r}; the cancel flag wins")
            )

        tracker = trackers_by_anchor.get(str(record.get(BOOKING_TRACKER_FIELD) or ""))
        url = _text(record, BOOKING_VENUE_FIELD)
        raw_room = record.get(ROOM_NAME_FIELD)
        if raw_room and room_id(raw_room) is None:
            junk_rooms.append(
                Disagreement(anchor, f"room name {str(raw_room)[:24]!r} is a display string")
            )

        if tracker is not None:
            absorbed.append(legacy_anchor(tracker))
            tracker_start = _timestamp(
                tracker, TRACKER_START_FIELD, assume=export_timezone, anchor=anchor
            )
            if tracker_start is not None and tracker_start != starts_at:
                starts_disagreements.append(
                    Disagreement(
                        anchor, f"tracker says {tracker_start:%Y-%m-%d %H:%M}Z; booking wins"
                    )
                )
            participants.extend(
                _participants(tracker, anchor, mentor, mentee, assume=export_timezone)
            )

        sessions.append(
            SessionRow(
                legacy_bubble_id=anchor,
                mentor_bubble_id=mentor,
                mentee_bubble_id=mentee,
                status=status,
                starts_at=starts_at,
                duration_minutes=minutes,
                topic=_text(record, BOOKING_TOPIC_FIELD),
                booking_message=_text(record, BOOKING_MESSAGE_FIELD),
                meeting_provider=meeting_provider(url),
                meeting_url=url,
                external_room_id=room_id(raw_room),
                external_calendar_event_id=_text(record, BOOKING_CALENDAR_EVENT_FIELD),
                created_at=created_at,
                updated_at=modified_at,
            )
        )

        # Creation, always. The mentee claimed the slot — `Session Initiator` is
        # exactly that link and is populated on every row.
        if created_at is not None:
            events.append(
                SessionEventRow(
                    session_bubble_id=anchor,
                    from_status=None,
                    to_status=SessionStatus.PENDING_MENTOR_APPROVAL,
                    actor_bubble_id=mentee,
                    actor_type=ActorType.USER,
                    reason_text=None,
                    created_at=created_at,
                )
            )

        # Terminal, unless it never transitioned.
        if status is SessionStatus.PENDING_MENTOR_APPROVAL or modified_at is None:
            continue
        accepted = _is_true(record.get(BOOKING_ACCEPTED_FIELD))
        if accepted and status is not SessionStatus.CONFIRMED:
            unplaceable.append(anchor)
        cancelled_by = _text(record, BOOKING_CANCELLED_BY_FIELD)
        actor, actor_type = _terminal_actor(status, cancelled_by)
        if status is SessionStatus.CANCELLED and modified_at > starts_at:
            cancel_late.append(
                Disagreement(anchor, "cancellation timestamp post-dates the session")
            )
        events.append(
            SessionEventRow(
                session_bubble_id=anchor,
                from_status=(
                    SessionStatus.CONFIRMED if accepted else SessionStatus.PENDING_MENTOR_APPROVAL
                ),
                to_status=status,
                actor_bubble_id=actor if actor in known_users else None,
                actor_type=actor_type,
                reason_text=_text(record, BOOKING_CANCEL_MESSAGE_FIELD),
                created_at=modified_at,
            )
        )

    # --------------------------------------------------------- orphan trackers
    merged = set(absorbed)
    for record in trackers:
        anchor = legacy_anchor(record)
        if anchor in merged:
            continue
        try:
            starts_at = _timestamp(
                record, TRACKER_START_FIELD, assume=export_timezone, anchor=anchor
            )
            created_at = _timestamp(record, CREATED_AT, assume=export_timezone, anchor=anchor)
            modified_at = _timestamp(record, MODIFIED_AT, assume=export_timezone, anchor=anchor)
        except TransformError as exc:
            errors.append(str(exc))
            continue

        mentor = _text(record, TRACKER_MENTOR_FIELD)
        creator = _text(record, CREATOR_FIELD)
        mentee = _text(record, TRACKER_MENTEE_FIELD) or (
            creator if creator in known_users else None
        )
        minutes = _duration(record.get(TRACKER_DURATION_FIELD))

        # Quarantine and drop mean different things and are kept apart: a
        # quarantined row *cannot be attributed* — nobody can say whose session
        # it was — while a dropped row is fully attributed and fails a column
        # constraint. Folding the duration check in here would bury a CHECK
        # violation inside "unattributable", which is the one report an operator
        # reads to decide whether legacy data needs fixing at source.
        missing = [
            name
            for name, value in (("mentor", mentor), ("mentee", mentee), ("start", starts_at))
            if not value
        ]
        if missing:
            quarantined.append(
                QuarantinedTracker(anchor, mentor, starts_at, f"no {', no '.join(missing)}")
            )
            continue
        if minutes is None:
            dropped.append(
                DroppedRow(
                    anchor, f"duration {record.get(TRACKER_DURATION_FIELD)!r} fails the CHECK"
                )
            )
            continue
        if mentor == mentee:
            dropped.append(DroppedRow(anchor, "mentor and mentee are the same user"))
            continue

        assert mentor is not None and mentee is not None  # noqa: S101 - narrowed by `missing`
        assert starts_at is not None and minutes is not None  # noqa: S101
        if mentor not in mentor_linked:
            mentors_without_link.append(mentor)
        cancelled = _is_true(record.get(TRACKER_CANCELLED_FIELD))
        url = _text(record, TRACKER_LINK_FIELD)
        raw_room = record.get(ROOM_NAME_FIELD)
        if raw_room and room_id(raw_room) is None:
            junk_rooms.append(
                Disagreement(anchor, f"room name {str(raw_room)[:24]!r} is a display string")
            )

        sessions.append(
            SessionRow(
                legacy_bubble_id=anchor,
                mentor_bubble_id=mentor,
                mentee_bubble_id=mentee,
                status=SessionStatus.CANCELLED if cancelled else SessionStatus.COMPLETED,
                starts_at=starts_at,
                duration_minutes=minutes,
                topic=None,
                booking_message=None,
                meeting_provider=meeting_provider(url),
                meeting_url=url if url and url.startswith("http") else None,
                external_room_id=room_id(raw_room),
                external_calendar_event_id=None,
                created_at=created_at,
                updated_at=modified_at,
                from_orphan_tracker=True,
            )
        )
        participants.extend(_participants(record, anchor, mentor, mentee, assume=export_timezone))

        # A cancel is a known transition; `Canceled = 'no'` means nothing
        # definite and gets no event. There is no creation event either — an
        # orphan belongs to the generation before the booking link existed, and
        # its creation semantics are unverified.
        if cancelled and modified_at is not None:
            events.append(
                SessionEventRow(
                    session_bubble_id=anchor,
                    from_status=None,
                    to_status=SessionStatus.CANCELLED,
                    actor_bubble_id=None,
                    actor_type=ActorType.USER,
                    reason_text=None,
                    created_at=modified_at,
                )
            )

    # ----------------------------------------------------------- session types
    defaulted: list[str] = []
    booking_missing: list[str] = []
    session_types: list[SessionTypeRow] = []
    for mentor in sorted({row.mentor_bubble_id for row in sessions}):
        minutes = durations.get(mentor)
        if minutes is None:
            defaulted.append(mentor)
            minutes = DEFAULT_DURATION_MINUTES
        # A mentor with sessions but no mentor record cannot happen — sessions
        # are keyed on a mentor-linked user — but the export is junk-filled test
        # data and this loop must not raise on a missing key. Falling back to the
        # column defaults is what the database would have done anyway.
        booking = booking_by_mentor.get(mentor)
        if booking is None:
            booking_missing.append(mentor)
            booking = (MeetingProvider.GOOGLE_MEET, False)
        venue, confirmation = booking
        session_types.append(
            SessionTypeRow(
                mentor_bubble_id=mentor,
                name=GENERAL_MENTORSHIP,
                duration_minutes=minutes,
                meeting_venue=venue,
                requires_booking_confirmation=confirmation,
            )
        )

    return SessionPlan(
        sessions=tuple(sessions),
        participants=tuple(participants),
        events=tuple(events),
        session_types=tuple(session_types),
        quarantined=tuple(quarantined),
        dropped=tuple(dropped),
        absorbed_trackers=tuple(absorbed),
        creator_mismatches=tuple(creator_mismatches),
        starts_at_disagreements=tuple(starts_disagreements),
        cancellation_overrode_status=tuple(cancel_overrode),
        cancel_time_after_session=tuple(cancel_late),
        unplaceable_confirmations=tuple(unplaceable),
        junk_room_names=tuple(junk_rooms),
        duration_disagreements=tuple(duration_disagreements),
        duration_defaulted=tuple(defaulted),
        booking_defaulted=tuple(booking_missing),
        reschedule_evidence=tuple(reschedules),
        mentors_without_link=tuple(sorted(set(mentors_without_link))),
        overlapping_live_windows=tuple(overlapping_windows(sessions)),
        source_booking_ids=tuple(source_booking_ids),
        source_tracker_ids=tuple(source_tracker_ids),
        errors=tuple(errors),
    )

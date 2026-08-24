"""What the platform tells people, and which of them hears it.

**One rule generates almost the whole table** (ADR 0025):

> A message caused by somebody goes to the party who did **not** cause it. A
> message caused by time going past goes to **both**.

Legacy told both parties everything, which tells each of them something they
already know: after an auto-confirming booking the mentee is looking at a
confirmation screen, and after an acceptance the mentor has just clicked the
button. The rule is written down because a vocabulary somebody maintains by hand
drifts, and because the exception below is only visibly principled beside it.

**The two confirmations are the same rule applied twice, not two rules.** An
auto-confirming booking tells the *mentor*; an acceptance tells the *mentee*.
They also differ in content — the mentee's carries the join link, the mentor's
says a booking arrived — which is why they are separate members rather than one
with a flag.

**Only messages with a producer are here.** Reviews, credits and vision boards
have none, so a member for them would be a template id nobody could send and a
vocabulary asserting a feature that does not exist (#21). Reminders are the one
exception and are named, because their schedule is settled and their absence is
the thing most likely to be mistaken for an oversight.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

__all__ = [
    "AUDIENCE",
    "REMINDER_OFFSETS",
    "REVIEW_REMINDER_AFTER",
    "REVIEW_REMINDER_KIND",
    "REVIEW_REMINDER_KINDS",
    "SESSION_REMINDERS",
    "SESSION_REMINDER_KINDS",
    "Audience",
    "Channel",
    "Notification",
    "SessionReminder",
    "recipients",
    "reminders_for",
    "session_reminders_for",
]


class Notification(StrEnum):
    """One thing the platform says. The key of the template map."""

    #: Booked on an offering that confirms itself. The mentee is looking at a
    #: confirmation screen; the mentor is the one learning something.
    SESSION_BOOKED = "session_booked"

    #: Booked on an offering that waits. Carries the deadline, because the
    #: mentor's slot is held until it passes.
    SESSION_REQUESTED = "session_requested"

    #: The mentor said yes. The mentee has been waiting on exactly this.
    REQUEST_ACCEPTED = "request_accepted"

    #: The mentor said no.
    REQUEST_DECLINED = "request_declined"

    #: The mentee took the request back before it was answered.
    REQUEST_WITHDRAWN = "request_withdrawn"

    #: A session both parties had agreed to is off. Either of them may do this,
    #: which makes it the only member whose audience is not knowable from the
    #: member alone.
    SESSION_CANCELLED = "session_cancelled"

    #: Nobody answered in time. **Both are told**, because nobody acted.
    REQUEST_EXPIRED = "request_expired"

    #: The application was approved. Not a session, so the audience table below
    #: does not reach it — see `recipients`.
    MENTOR_APPROVED = "mentor_approved"

    #: A mentor's connected calendar stopped working — revoked in Google's own
    #: settings, or a token that aged out unused. **Not a session either**, so
    #: `AUDIENCE` does not reach it: there is one recipient and no other party
    #: to choose between.
    #:
    #: Sent once, when the connection moves to `error`. The sweep that finds
    #: these runs hourly and would otherwise say the same thing every hour —
    #: `record_failure` only matches an `active` row, so the second attempt
    #: writes nothing and tells nobody.
    CALENDAR_DISCONNECTED = "calendar_disconnected"

    #: A confirmed session is coming up. **Both parties**, because either can
    #: forget, and the legacy application sends this — not having it would be a
    #: regression against the app being replaced rather than a missing extra.
    SESSION_REMINDER = "session_reminder"

    #: The same, close enough that there is no time to rearrange. A separate
    #: member rather than the one above with a different interval, because the
    #: templates differ: this one is about turning up, that one about preparing.
    SESSION_LAST_REMINDER = "session_last_reminder"

    #: **The mentee is asked how the session went.** Fired by the transition into
    #: `completed`, not by a clock: `settle_attendance` decides `completed`
    #: against `no_show` from attendance, so a session nobody turned up to is
    #: structurally incapable of asking for a review. A timer set to "end plus
    #: ten minutes" would race that sweep and sometimes win.
    #:
    #: `Audience.MENTEE`, and that is the whole reason the message this replaces
    #: was withdrawn: it conflated a *platform* survey to both parties with a
    #: *mentor review* from the mentee, and shipping it would have asked a mentor
    #: to review the session they gave.
    #:
    #: **The platform survey is still not built.** It may come back; it is not
    #: this, and #21 says a vocabulary does not carry a member for a feature
    #: nobody is building.
    REVIEW_REQUESTED = "review_requested"

    #: One nudge, a day later, and only while it is still owed. Not cancelled
    #: when the review arrives — **checked at fire time**, which is what the
    #: callback module means by "scheduling ahead safe without ever cancelling
    #: anything" (ADR 0025). Cancelling would make the write path responsible
    #: for unscheduling, and the bug is the reminder that fires for a review
    #: written through the one path somebody forgot.
    REVIEW_REMINDER = "review_reminder"

    #: Somebody applied to be a mentor. **To the admins, not to a party of a
    #: session**, so `AUDIENCE` does not reach it — the recipients are a *set*
    #: resolved from live grants rather than one person named on a row, which is
    #: the first message here with that shape.
    #:
    #: Without it an application is found by somebody thinking to look, which is
    #: how a queue grows quietly.
    MENTOR_APPLICATION_RECEIVED = "mentor_application_received"

    #: And declined, carrying whatever reason the reviewer gave.
    MENTOR_DECLINED = "mentor_declined"

    #: **Settled and unbuilt.** Three sends per pending request — on booking, 24
    #: hours before `respond_by` where the lead allows it, and 12 hours before.
    #: Named here so its absence reads as deferred rather than forgotten; it
    #: needs QStash and a callback endpoint, which arrive together.
    MENTOR_RESPONSE_REMINDER = "mentor_response_reminder"


class Audience(StrEnum):
    """Who hears a session message.

    Four values rather than two ids, because three of them are knowable from the
    message alone and one is not — and collapsing that difference would mean
    every call site passing an actor it usually does not need.
    """

    MENTOR = "mentor"
    MENTEE = "mentee"
    #: Whichever party did not act. `SESSION_CANCELLED` alone, because it is the
    #: only action either party may take.
    OTHER_PARTY = "other_party"
    #: Nobody acted, so nobody already knows.
    BOTH = "both"


#: The rule, as a table. Absent means the message is not about a session.
AUDIENCE: dict[Notification, Audience] = {
    Notification.SESSION_BOOKED: Audience.MENTOR,
    Notification.SESSION_REQUESTED: Audience.MENTOR,
    Notification.REQUEST_ACCEPTED: Audience.MENTEE,
    Notification.REQUEST_DECLINED: Audience.MENTEE,
    Notification.REQUEST_WITHDRAWN: Audience.MENTOR,
    Notification.SESSION_CANCELLED: Audience.OTHER_PARTY,
    Notification.REQUEST_EXPIRED: Audience.BOTH,
    Notification.MENTOR_RESPONSE_REMINDER: Audience.MENTOR,
    Notification.SESSION_REMINDER: Audience.BOTH,
    #: **Both to the mentee, and neither to the mentor.** A review is one
    #: party's account of the other's work; telling the subject it has been
    #: requested is a different message that nobody has asked for.
    Notification.REVIEW_REQUESTED: Audience.MENTEE,
    Notification.REVIEW_REMINDER: Audience.MENTEE,
    Notification.SESSION_LAST_REMINDER: Audience.BOTH,
}


def recipients(
    notification: Notification,
    *,
    mentor_id: UUID,
    mentee_id: UUID,
    actor_id: UUID | None = None,
) -> tuple[UUID, ...]:
    """Who to tell, in a stable order.

    ``actor_id`` is needed only by :attr:`Audience.OTHER_PARTY` and is ``None``
    for everything else — including every message a sweep produces, where the
    honest answer is that nobody acted.

    **A cancellation with no actor tells both.** That is unreachable through the
    API, where cancelling requires a caller, and it is the right answer if
    anything ever reaches it: telling both is a message somebody did not need,
    where telling neither is a session called off in silence.

    Raises :class:`KeyError` for a message that is not about a session, which is
    deliberate — `MENTOR_APPROVED` has no mentor and mentee to choose between,
    and returning an empty tuple would let a caller send it to nobody and not
    notice.
    """
    audience = AUDIENCE[notification]
    if audience is Audience.MENTOR:
        return (mentor_id,)
    if audience is Audience.MENTEE:
        return (mentee_id,)
    if audience is Audience.BOTH:
        return (mentor_id, mentee_id)
    if actor_id == mentor_id:
        return (mentee_id,)
    if actor_id == mentee_id:
        return (mentor_id,)
    return (mentor_id, mentee_id)


class Channel(StrEnum):
    """How it is delivered.

    **Email reaches everybody today; WhatsApp reaches nobody**, because the three
    ``phone_*`` columns are deferred and the migration runbook lists phone
    collection as post-cutover work. So a message declared for both channels is
    delivered on one for now, and that is a fact about the data rather than
    about the code.

    ADR 0006 bounds the WhatsApp side: platform-to-user transactional only.
    Mentor-to-mentee conversation stays in-platform, because handing both sides
    of a paid booking a direct off-platform channel is disintermediation by
    default rather than by decision.
    """

    EMAIL = "email"
    WHATSAPP = "whatsapp"


def unique(ids: Iterable[UUID]) -> tuple[UUID, ...]:
    """Order-preserving de-duplication, for a caller assembling an audience.

    Not used by :func:`recipients`, whose outputs cannot repeat — ``mentor_id``
    and ``mentee_id`` differ by the ``no_self_booking`` check. It exists for the
    caller that unions two audiences, where the same person can legitimately
    appear twice.
    """
    seen: dict[UUID, None] = {}
    for value in ids:
        seen.setdefault(value, None)
    return tuple(seen)


#: How long before the deadline each reminder fires, keyed by a name that goes
#: on the wire and into the outbox row.
#:
#: **Measured back from `respond_by`, not forward from the booking**, and that is
#: the only well-formed reading: the minimum gap from booking to deadline is
#: eighteen hours, so a reminder twenty-four hours *after* booking would fire
#: after the deadline had already passed on every minimum-notice request.
#:
#: `on_booking` is absent because it is not scheduled — it is enqueued directly
#: by the booking that causes it, which needs no scheduler and cannot be missed.
REMINDER_OFFSETS: dict[str, dt.timedelta] = {
    "t24": dt.timedelta(hours=24),
    "t12": dt.timedelta(hours=12),
}


@dataclass(frozen=True, slots=True)
class SessionReminder:
    """One nudge before a session: when it fires, and what it says it is."""

    #: What the callback carries, and what the outbox dedups on. Prefixed `s`
    #: so a session reminder can never be mistaken for a response reminder —
    #: the two mean different things and require different statuses.
    kind: str
    notification: Notification
    before: dt.timedelta
    #: The words the template renders for `intervaltime`. Here rather than in a
    #: resolver because it is a property of the schedule: change the offset and
    #: the wording has to move with it, and separating them is how a message
    #: ends up saying "24 hours" an hour before.
    interval: str


#: When a confirmed session is nudged. **Twenty-four hours and one hour.**
#:
#: The first is far enough out to prepare or to rearrange; the second is close
#: enough that it is about turning up. The booking floor is also 24 hours, so a
#: session booked at the minimum notice has its 24-hour reminder already behind
#: it — dropped rather than fired, for the reason :func:`reminders_for` gives.
SESSION_REMINDERS: tuple[SessionReminder, ...] = (
    SessionReminder("s24", Notification.SESSION_REMINDER, dt.timedelta(hours=24), "24 hours"),
    SessionReminder("s1", Notification.SESSION_LAST_REMINDER, dt.timedelta(hours=1), "1 hour"),
)

#: The kinds a callback may carry, and what each one is about. Looked up rather
#: than parsed, so a kind nobody defined is refused at the door instead of
#: reaching a handler that has to guess.
SESSION_REMINDER_KINDS = {reminder.kind: reminder for reminder in SESSION_REMINDERS}

#: How long after the request the mentee is nudged once.
#:
#: **A day, and only one.** Long enough that the session is behind them and
#: short enough that they remember it; a second nudge for an unpaid, optional
#: favour is the shape people mute a sender over.
REVIEW_REMINDER_AFTER = dt.timedelta(hours=24)

#: What the review reminder's callback carries.
#:
#: **Prefixed `r`, for the reason `s` exists.** `t24` is a response reminder
#: and `s24` a session reminder, and the three mean different things with
#: different conditions for still being owed. A bare `24` would let one
#: callback fire another's rule.
REVIEW_REMINDER_KIND = "r24"

#: A set of one, mirroring `SESSION_REMINDER_KINDS`. A registry rather than an
#: equality check, so a second review reminder joins it instead of adding a
#: branch to the callback that dispatches on it.
REVIEW_REMINDER_KINDS = frozenset({REVIEW_REMINDER_KIND})


def session_reminders_for(
    starts_at: dt.datetime, *, now: dt.datetime
) -> tuple[tuple[SessionReminder, dt.datetime], ...]:
    """Which session reminders are still ahead, and when each fires.

    Same rule as :func:`reminders_for`: one whose moment has passed is dropped
    rather than fired immediately. At the 24-hour booking floor that is the
    24-hour reminder, and sending it on confirmation would be the confirmation
    message again, saying the session is tomorrow when the mentee just booked it
    for tomorrow.
    """
    return tuple(
        (reminder, starts_at - reminder.before)
        for reminder in SESSION_REMINDERS
        if starts_at - reminder.before > now
    )


def reminders_for(
    respond_by: dt.datetime, *, now: dt.datetime
) -> tuple[tuple[str, dt.datetime], ...]:
    """Which reminders are still ahead, and when each fires.

    **A reminder whose moment has already passed is dropped rather than fired
    immediately.** At the 24-hour booking floor the deadline is eighteen hours
    away, so `t24` is already behind — sending it the instant the booking is
    made would be a second copy of the on-booking message, thirty seconds later,
    saying the same thing.

    Returns pairs rather than a mapping so the order is the order they fire in,
    which is what a reader checking a schedule wants to see.
    """
    return tuple(
        (kind, respond_by - offset)
        for kind, offset in REMINDER_OFFSETS.items()
        if respond_by - offset > now
    )

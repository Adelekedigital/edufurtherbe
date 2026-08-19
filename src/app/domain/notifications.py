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

from collections.abc import Iterable
from enum import StrEnum
from uuid import UUID

__all__ = [
    "AUDIENCE",
    "Audience",
    "Channel",
    "Notification",
    "recipients",
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

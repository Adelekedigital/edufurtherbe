"""Every email an admin sees in the admin app, and the values its lines may use (PRD 28).

**Engineering's list, not the database's.** Admins reword lines; they never add
or delete an email. The list, the editor, the save check and the send path all
read the facts here, beside `LINES` (decision 317):

- `EMAILS`: one entry per `Notification`, in the order the admin list shows them,
  with its name, trigger, card and recipients. Its variants are `LINES`' cases.
- `VALUES`: the values a line may name as ``{name}``, with what each means, an
  example, and what an empty one becomes in a line at send time (decision 319).
- `SAMPLES`: what each email's producer puts on its outbox row, so the preview,
  the as-sent count, `values_for` and the tests all fill from one table.

Whether an email is Live (its template is the shared one) is configuration and
Loops, so it is worked out at the edge, not here.
"""

from __future__ import annotations

import datetime as dt
import difflib
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.domain.email_lines import (
    LINE_HARD_MAX,
    LINES,
    PLACEHOLDER,
    TEMPLATE_GROUPS,
    variant_of,
)
from app.domain.enums import EmailLine
from app.domain.message_emails import (
    ONE_MESSAGE,
    TWO_FROM_ONE,
    TWO_FROM_TWO,
    EmailedMessage,
    instant_email,
    unread_email,
)
from app.domain.messages import ALIASES, MessageContext, UnresolvedVariableError, resolve
from app.domain.notifications import AUDIENCE, Audience, Notification

__all__ = [
    "EMAILS",
    "SAMPLES",
    "VALUES",
    "VARIANT_LABELS",
    "EmailEntry",
    "LineError",
    "LineErrorCode",
    "Recipient",
    "Value",
    "check_line",
    "entry",
    "sample_context",
    "values_for",
    "variants_of",
]

N = Notification


class Recipient(StrEnum):
    """Who an email goes to, as the admin list's "Goes to" filter reads it."""

    MENTOR = "mentor"
    MENTEE = "mentee"
    ADMINS = "admins"


@dataclass(frozen=True, slots=True)
class EmailEntry:
    """One email as the admin app shows it."""

    key: Notification
    name: str
    trigger: str
    template_name: str
    recipients: tuple[Recipient, ...]

    @property
    def group(self) -> str:
        """Its shared template's key, or its own key for a template of its own."""
        return next(
            (key for key, members in TEMPLATE_GROUPS.items() if self.key in members),
            str(self.key),
        )


_BOTH = (Recipient.MENTOR, Recipient.MENTEE)
_SIDE = {
    Audience.MENTOR: (Recipient.MENTOR,),
    Audience.MENTEE: (Recipient.MENTEE,),
    Audience.BOTH: _BOTH,
    Audience.OTHER_PARTY: _BOTH,
}

#: What each card is called in the list: a shared template's group, by its key.
GROUP_NAMES = {
    "session_confirmed": "Session confirmed",
    "request_awaiting": "Requests awaiting an answer",
    "request_closed": "Requests that ended",
    "session_reminders": "Session reminders",
    "suggested_time": "Suggested times",
    "credits": "Credits",
    "mentor_status": "Mentor status",
    "messages": "Messages",
}


def _e(
    key: Notification,
    name: str,
    trigger: str,
    template: str,
    recipients: tuple[Recipient, ...] | None = None,
) -> EmailEntry:
    return EmailEntry(key, name, trigger, template, recipients or _SIDE[AUDIENCE[key]])


#: Every email, in the order the admin list shows them: a shared template's
#: emails together, its cards in this order.
EMAILS: tuple[EmailEntry, ...] = (
    _e(
        N.SESSION_BOOKED,
        "Session booked",
        "A booking is confirmed straight away",
        "Session confirmed",
    ),
    _e(N.REQUEST_ACCEPTED, "Request accepted", "A mentor accepts a request", "Session confirmed"),
    _e(N.SESSION_REQUESTED, "Session requested", "A mentee requests a session", "Request awaiting"),
    _e(
        N.MENTOR_RESPONSE_REMINDER,
        "Request reminder",
        "A request is close to its answer deadline",
        "Request awaiting",
    ),
    _e(
        N.MENTEE_REQUEST_REMINDER,
        "Mentee's reminder",
        "A mentee presses Remind on a pending request",
        "Request awaiting",
    ),
    _e(N.REQUEST_DECLINED, "Request declined", "A mentor declines a request", "Request closed"),
    _e(N.REQUEST_WITHDRAWN, "Request withdrawn", "A mentee withdraws a request", "Request closed"),
    _e(
        N.SESSION_REMINDER,
        "Session reminder",
        "24 hours, 1 hour and 30 minutes before a booking",
        "Session reminders",
    ),
    _e(N.SESSION_LAST_REMINDER, "Last reminder", "5 minutes before a booking", "Session reminders"),
    _e(
        N.SESSION_TIME_SUGGESTED,
        "New time suggested",
        "A mentor declines or cancels and suggests another time",
        "Suggested time",
    ),
    _e(
        N.SESSION_SUGGESTION_REMINDER,
        "Suggested time reminder",
        "30 minutes before a suggested time is released",
        "Suggested time",
    ),
    _e(
        N.CREDITS_GRANTED,
        "Credits granted",
        "A mentee finishes onboarding",
        "Credits",
        (Recipient.MENTEE,),
    ),
    _e(
        N.CREDITS_RENEWED,
        "Credits renewed",
        "The month's credits are paid",
        "Credits",
        (Recipient.MENTEE,),
    ),
    _e(
        N.CREDITS_EXPIRING,
        "Credits expiring",
        "Two weeks and one week before unused credits expire",
        "Credits",
        (Recipient.MENTEE,),
    ),
    _e(
        N.MENTOR_APPROVED,
        "Mentor approved",
        "An admin approves an application",
        "Mentor status",
        (Recipient.MENTOR,),
    ),
    _e(
        N.MENTOR_DECLINED,
        "Mentor declined",
        "An admin declines an application",
        "Mentor status",
        (Recipient.MENTOR,),
    ),
    _e(
        N.MENTOR_PAUSED_BY_ADMIN,
        "Mentor paused",
        "An admin pauses a mentor's listing",
        "Mentor status",
        (Recipient.MENTOR,),
    ),
    _e(
        N.MESSAGE_UNREAD_FEW,
        "Unread messages",
        "One or two messages unread for 15 minutes",
        "Messages",
        _BOTH,
    ),
    _e(
        N.MESSAGE_UNREAD_MANY,
        "Many unread messages",
        "Three or more messages unread for 15 minutes",
        "Messages",
        _BOTH,
    ),
    _e(
        N.MESSAGE_IN_SESSION_WINDOW,
        "Message close to a booking",
        "A message sent within 15 minutes of their booking",
        "Messages",
        _BOTH,
    ),
    _e(
        N.SESSION_CANCELLED,
        "Session canceled",
        "Either person cancels a booking",
        "Session Canceled",
    ),
    _e(N.REQUEST_EXPIRED, "Request expired", "A request gets no answer in time", "Request expired"),
    _e(N.REVIEW_REQUESTED, "Review requested", "A booking is completed", "sessionReviewRequest"),
    _e(N.REVIEW_RECEIVED, "Review received", "A mentee reviews a booking", "reviewUpdateToMentors"),
    _e(
        N.MENTOR_APPLICATION_RECEIVED,
        "New mentor application",
        "A mentor submits a complete application",
        "New mentor application",
        (Recipient.ADMINS,),
    ),
    _e(
        N.MENTOR_RETURN_REMINDER,
        "Return reminder",
        "Before a paused mentor's return date, or while paused with no date",
        "Return reminder",
        (Recipient.MENTOR,),
    ),
    _e(
        N.CALENDAR_DISCONNECTED,
        "Calendar disconnected",
        "A mentor's Google Calendar link stops working",
        "Calendar disconnected",
        (Recipient.MENTOR,),
    ),
)

_BY_KEY = {email.key: email for email in EMAILS}


def entry(key: str) -> EmailEntry | None:
    """The registered email for ``key``, or ``None``."""
    try:
        return _BY_KEY.get(Notification(key))
    except ValueError:
        return None


#: How a variant reads on its tab. ``default`` is the email's own lines.
VARIANT_LABELS = {
    "default": "Everyone it goes to",
    "mentor": "To the mentor",
    "mentee": "To the mentee",
    "admins": "To admins",
    ONE_MESSAGE: "1 message",
    TWO_FROM_ONE: "2 from one person",
    TWO_FROM_TWO: "From two people",
}


def variants_of(email: EmailEntry) -> tuple[str, ...]:
    """The email's variants, as the tables and the API name them."""
    return tuple(variant_of(case) for case in LINES.get(email.key, {"": None}))


@dataclass(frozen=True, slots=True)
class Value:
    """A value a line may name: what it means, an example, and what an empty one
    becomes in a line at send time (``None``: the line cannot go without it).

    ``needs`` is what the email must be about for the value to mean anything:
    ``"session"``, or row keys any one of which it carries. A value with a
    fallback of its own (a topic, a venue) fills anywhere, so filling alone
    cannot say whether an email carries it.
    """

    name: str
    meaning: str
    example: str
    fallback: str | None
    needs: tuple[str, ...] = ()


_SESSION = ("session",)

#: The values a line may name, in the order the picker lists them. Card slots,
#: links and the lines themselves are not words a line says.
VALUES: tuple[Value, ...] = (
    Value("recipientFirstName", "The reader's first name", "Ada", "there"),
    Value("recipientName", "The reader's full name", "Ada Obi", "there"),
    Value("mentorName", "The mentor's name", "Ada Obi", "your mentor"),
    Value("menteeName", "The mentee's name", "Mo Bello", "your mentee"),
    Value("attendee", "The other person in the booking", "Mo Bello", "the other person", _SESSION),
    Value(
        "sessionTopic",
        "What this booking is about",
        "Choosing a master's",
        "your session",
        _SESSION,
    ),
    Value("sessionName", "The session's name", "CV review", "your session", _SESSION),
    Value(
        "sessionDate",
        "The session's date, in the reader's zone",
        "Friday 20 November 2026",
        "the day",
        _SESSION,
    ),
    Value(
        "sessionTime",
        "The session's start time, in the reader's zone",
        "15:00",
        "the time",
        _SESSION,
    ),
    Value("sessionTimezone", "The reader's time zone", "Africa/Lagos", "your time zone", _SESSION),
    Value("sessionDuration", "Session length", "45 min", "the session", _SESSION),
    Value("location", "Where the session happens", "Google Meet", "online", _SESSION),
    Value("hours", "Whole hours left to answer a request", "18", "a few", _SESSION),
    Value("intervalTime", "How soon, in words", "Tomorrow", "soon"),
    Value(
        "reasonTitle",
        "Why it ended, in words",
        "Scheduling conflict",
        "no reason given",
        ("reason_code", "reason_text"),
    ),
    Value("cancelInitiator", "Who cancelled", "Ada Obi", "someone"),
    Value("suggestedDate", "The suggested time's date", "Saturday 21 November 2026", "the day"),
    Value("suggestedTime", "The suggested start time", "16:00", "the time"),
    Value("holdUntilTime", "When the suggested time is released", "17:30", "soon"),
    Value("creditCount", "How many credits", "2", "your"),
    Value(
        "returnOn",
        "The date a paused mentor returns",
        "Saturday 3 October 2026",
        "your return date",
    ),
    Value("daysUntilReturn", "Days until a paused mentor returns", "3", "a few"),
    Value("daysPaused", "Days a mentor has been paused", "30", "a while"),
    Value("repliesName", 'Who to reply to: a first name, or "them"', "Ada", "them"),
    Value("senderFirstName", "The sender's first name", "Ada", "someone"),
    Value("senderFirstNames", "The senders' first names", "Ada and Ben", "people"),
    Value("unreadCount", "How many messages are unread", "3", "new"),
)

_VALUE_NAMES = {value.name for value in VALUES}

#: When the sample session starts.
_START = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.UTC)
_SENDERS = {
    name: UUID(f"00000000-0000-7000-8000-00000000000{n}")
    for n, name in enumerate(("Ada", "Ben", "Chi"), 1)
}


def _said(first: str, minutes: int = 0) -> EmailedMessage:
    sent_at = _START - dt.timedelta(minutes=minutes)
    return EmailedMessage(
        _SENDERS[first], first, "Obi", "Hello, are you free this week?", None, sent_at
    )


#: What each email's producer puts on its outbox row (`variables=` at the
#: enqueue, or the message email's values), by variant.
SAMPLES: dict[Notification, dict[str, dict[str, str]]] = {
    N.SESSION_BOOKED: {"mentor": {}, "mentee": {}},
    N.REQUEST_ACCEPTED: {"default": {}},
    N.SESSION_REQUESTED: {"default": {}},
    N.MENTOR_RESPONSE_REMINDER: {"default": {"kind": "t24"}},
    N.MENTEE_REQUEST_REMINDER: {"default": {"reminder_message": "Still keen if you have time!"}},
    N.REQUEST_DECLINED: {"default": {"reason_code": "scheduling_conflict"}},
    N.REQUEST_WITHDRAWN: {"default": {"reason_code": "mentee_no_longer_needed"}},
    N.SESSION_REMINDER: {"default": {"kind": "s24", "interval": "Tomorrow"}},
    N.SESSION_LAST_REMINDER: {"default": {"kind": "s5", "interval": "in 5 Minutes"}},
    N.SESSION_TIME_SUGGESTED: {
        "default": {
            "suggested_after": "declined",
            "suggested_starts_at": (_START + dt.timedelta(days=1, hours=2)).isoformat(),
            "held_until": (_START + dt.timedelta(hours=3, minutes=30)).isoformat(),
        }
    },
    N.SESSION_SUGGESTION_REMINDER: {
        "default": {
            "suggested_starts_at": (_START + dt.timedelta(days=1, hours=2)).isoformat(),
            "held_until": (_START + dt.timedelta(hours=3, minutes=30)).isoformat(),
        }
    },
    N.CREDITS_GRANTED: {"default": {}},
    N.CREDITS_RENEWED: {"default": {"credit_count": "2"}},
    N.CREDITS_EXPIRING: {"default": {"kind": "c14", "interval": "two weeks", "credit_count": "1"}},
    N.MENTOR_APPROVED: {"default": {}},
    N.MENTOR_DECLINED: {"default": {"reason": "Add a little more about your experience."}},
    N.MENTOR_PAUSED_BY_ADMIN: {"default": {}},
    N.MESSAGE_UNREAD_FEW: {
        ONE_MESSAGE: unread_email([_said("Ada")]),
        TWO_FROM_ONE: unread_email([_said("Ada", 2), _said("Ada")]),
        TWO_FROM_TWO: unread_email([_said("Ada", 2), _said("Ben")]),
    },
    N.MESSAGE_UNREAD_MANY: {
        "default": unread_email([_said("Ada", 3), _said("Ben", 2), _said("Chi")])
    },
    N.MESSAGE_IN_SESSION_WINDOW: {"default": instant_email(_said("Ada"))},
}

#: What each email's context is built from, as the outbox builds it
#: (`_context_for`): a session carries one and both names; a mentor's profile
#: names the mentor alone; a user or a chat names nobody.
_ABOUT_A_SESSION = frozenset(AUDIENCE) | {N.MESSAGE_IN_SESSION_WINDOW}
_ABOUT_A_MENTOR = frozenset(
    {
        N.MENTOR_APPROVED,
        N.MENTOR_DECLINED,
        N.MENTOR_PAUSED_BY_ADMIN,
        N.MENTOR_RETURN_REMINDER,
        N.MENTOR_APPLICATION_RECEIVED,
    }
)


def sample_context(email: Notification, variant: str) -> MessageContext:
    """The context the email's ``variant`` is previewed and checked against:
    its producer's sample row, shaped as the outbox shapes that email's."""
    extras = dict(SAMPLES.get(email, {}).get(variant, {}))
    role = variant if variant in {"mentor", "mentee"} else ""
    session = email in _ABOUT_A_SESSION
    mentor = session or email in _ABOUT_A_MENTOR
    reader = (
        ("Ada Obi", "Ada") if role == "mentor" or email in _ABOUT_A_MENTOR else ("Mo Bello", "Mo")
    )
    return MessageContext(
        recipient_name=reader[0],
        recipient_first_name=reader[1],
        recipient_timezone="Africa/Lagos",
        mentor_name="Ada Obi" if mentor else "",
        mentee_name="Mo Bello" if session else "",
        starts_at=_START if session else None,
        topic="Choosing a master's" if session else None,
        session_type_name="CV review" if session else None,
        venue="Google Meet" if session else None,
        respond_by=_START - dt.timedelta(hours=6) if session else None,
        duration_minutes=45 if session else None,
        session_id="00000000-0000-7000-8000-0000000000aa" if session else None,
        app_base_url="https://app.edufurther.example",
        extras=extras,
        notification=email,
        recipient_role=role,
    )


def values_for(email: Notification, variant: str) -> tuple[str, ...]:
    """The `VALUES` a line of this variant may name: those its sample fills."""
    context = sample_context(email, variant)
    found = []
    for value in VALUES:
        if value.needs and not any(
            context.starts_at is not None if need == "session" else need in context.extras
            for need in value.needs
        ):
            continue
        try:
            if resolve(value.name, context):
                found.append(value.name)
        except UnresolvedVariableError:
            continue
    return tuple(found)


class LineErrorCode(StrEnum):
    """Why a line cannot be saved (PRD 28, the 422's ``errors[].code``)."""

    EMPTY = "empty"
    UNKNOWN_VALUE = "unknown_value"
    VALUE_NOT_SENT = "value_not_sent"
    TOO_LONG = "too_long"


@dataclass(frozen=True, slots=True)
class LineError:
    """One reason, with the value it is about and a suggested name if any."""

    line: EmailLine
    code: LineErrorCode
    value: str | None = None
    suggestion: str | None = None


def _suggest(name: str) -> str | None:
    """The catalogue name ``name`` most likely meant: the same ignoring case,
    else the closest spelling."""
    for known in _VALUE_NAMES:
        if known.lower() == name.lower():
            return known
    close = difflib.get_close_matches(name, sorted(_VALUE_NAMES), n=1, cutoff=0.8)
    return close[0] if close else None


def check_line(email: Notification, variant: str, line: EmailLine, text: str) -> list[LineError]:
    """Every reason ``text`` cannot be saved as this line, or none.

    The hard max counts the raw text, ``{name}``s included. A name a line may
    not say (a link, a card slot, a line itself) is not in `VALUES`, so it is as
    unknown as a misspelling. A value another email of the group owns does not
    fill here (`resolve`), so it is not carried.
    """
    if not text.strip():
        return [LineError(line, LineErrorCode.EMPTY)]
    errors = []
    if len(text) > LINE_HARD_MAX[line]:
        errors.append(LineError(line, LineErrorCode.TOO_LONG))
    carried = values_for(email, variant)
    for name in dict.fromkeys(PLACEHOLDER.findall(text)):
        canonical = ALIASES.get(name, name)
        if canonical not in _VALUE_NAMES:
            errors.append(LineError(line, LineErrorCode.UNKNOWN_VALUE, name, _suggest(name)))
        elif canonical not in carried:
            errors.append(LineError(line, LineErrorCode.VALUE_NOT_SENT, name))
    return errors

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
import functools
import re
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
from app.domain.email_values import FALLBACKS, VALUE_NAMES, VALUES, Value
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
    "FALLBACKS",
    "GROUP_NAMES",
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


#: Each shared template's name, by its group's key: the list card's title and
#: the Loops template's name, which are one name.
GROUP_NAMES = {
    "session_confirmed": "Session confirmed",
    "request_awaiting": "Request awaiting an answer",
    "request_closed": "Request ended",
    "session_reminders": "Session reminders",
    "suggested_time": "Suggested time",
    "credits": "Credits",
    "mentor_status": "Mentor status",
    "messages": "Messages",
}


@dataclass(frozen=True, slots=True)
class EmailEntry:
    """One email as the admin app shows it. ``own_template`` names the Loops
    template of an email outside every group."""

    key: Notification
    name: str
    trigger: str
    recipients: tuple[Recipient, ...]
    own_template: str | None = None

    @property
    def group(self) -> str:
        """Its shared template's key, or its own key for a template of its own."""
        return next(
            (key for key, members in TEMPLATE_GROUPS.items() if self.key in members),
            str(self.key),
        )

    @property
    def group_name(self) -> str:
        return GROUP_NAMES.get(self.group, self.name)

    @property
    def template_name(self) -> str:
        return GROUP_NAMES.get(self.group) or self.own_template or self.name


_BOTH = (Recipient.MENTOR, Recipient.MENTEE)
_SIDE = {
    Audience.MENTOR: (Recipient.MENTOR,),
    Audience.MENTEE: (Recipient.MENTEE,),
    Audience.BOTH: _BOTH,
    Audience.OTHER_PARTY: _BOTH,
}
_MENTOR = (Recipient.MENTOR,)
_MENTEE = (Recipient.MENTEE,)


def _e(
    key: Notification,
    name: str,
    trigger: str,
    recipients: tuple[Recipient, ...] | None = None,
    own_template: str | None = None,
) -> EmailEntry:
    return EmailEntry(key, name, trigger, recipients or _SIDE[AUDIENCE[key]], own_template)


#: Every email, in the order the admin list shows them: a shared template's
#: emails together, its cards in this order.
EMAILS: tuple[EmailEntry, ...] = (
    _e(N.SESSION_BOOKED, "Session booked", "A booking is confirmed straight away"),
    _e(N.REQUEST_ACCEPTED, "Request accepted", "A mentor accepts a request"),
    _e(N.SESSION_REQUESTED, "Session requested", "A mentee requests a session"),
    _e(N.MENTOR_RESPONSE_REMINDER, "Request reminder", "A request is close to its answer deadline"),
    _e(
        N.MENTEE_REQUEST_REMINDER,
        "Mentee's reminder",
        "A mentee presses Remind on a pending request",
    ),
    _e(N.REQUEST_DECLINED, "Request declined", "A mentor declines a request"),
    _e(N.REQUEST_WITHDRAWN, "Request withdrawn", "A mentee withdraws a request"),
    _e(
        N.SESSION_REMINDER,
        "Session reminder",
        "24 hours, 1 hour and 30 minutes before a booking",
    ),
    _e(N.SESSION_LAST_REMINDER, "Last reminder", "5 minutes before a booking"),
    _e(
        N.SESSION_TIME_SUGGESTED,
        "New time suggested",
        "A mentor declines or cancels and suggests another time",
    ),
    _e(
        N.SESSION_SUGGESTION_REMINDER,
        "Suggested time reminder",
        "30 minutes before a suggested time is released",
    ),
    _e(N.CREDITS_GRANTED, "Credits granted", "A mentee finishes onboarding", _MENTEE),
    _e(N.CREDITS_RENEWED, "Credits renewed", "The month's credits are paid", _MENTEE),
    _e(
        N.CREDITS_EXPIRING,
        "Credits expiring",
        "Two weeks and one week before unused credits expire",
        _MENTEE,
    ),
    _e(N.MENTOR_APPROVED, "Mentor approved", "An admin approves an application", _MENTOR),
    _e(N.MENTOR_DECLINED, "Mentor declined", "An admin declines an application", _MENTOR),
    _e(N.MENTOR_PAUSED_BY_ADMIN, "Mentor paused", "An admin pauses a mentor's listing", _MENTOR),
    _e(N.MESSAGE_UNREAD_FEW, "Unread messages", "One or two messages unread for 15 minutes", _BOTH),
    _e(
        N.MESSAGE_UNREAD_MANY,
        "Many unread messages",
        "Three or more messages unread for 15 minutes",
        _BOTH,
    ),
    _e(
        N.MESSAGE_IN_SESSION_WINDOW,
        "Message close to a booking",
        "A message sent within 15 minutes of their booking",
        _BOTH,
    ),
    _e(
        N.SESSION_CANCELLED,
        "Session canceled",
        "Either person cancels a booking",
        own_template="Session Canceled",
    ),
    _e(N.REQUEST_EXPIRED, "Request expired", "A request gets no answer in time"),
    _e(
        N.REVIEW_REQUESTED,
        "Review requested",
        "A booking is completed",
        own_template="sessionReviewRequest",
    ),
    _e(
        N.REVIEW_RECEIVED,
        "Review received",
        "A mentee reviews a booking",
        own_template="reviewUpdateToMentors",
    ),
    _e(
        N.MENTOR_APPLICATION_RECEIVED,
        "New mentor application",
        "A mentor submits a complete application",
        (Recipient.ADMINS,),
    ),
    _e(
        N.MENTOR_RETURN_REMINDER,
        "Return reminder",
        "Before a paused mentor's return date, or while paused with no date",
        _MENTOR,
    ),
    _e(
        N.CALENDAR_DISCONNECTED,
        "Calendar disconnected",
        "A mentor's Google Calendar link stops working",
        _MENTOR,
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


#: The emails a request's answer deadline means something in.
_REQUESTS = TEMPLATE_GROUPS["request_awaiting"] | {N.REQUEST_EXPIRED}


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


_SUGGESTED = {
    "suggested_starts_at": (_START + dt.timedelta(days=1, hours=2)).isoformat(),
    "held_until": (_START + dt.timedelta(hours=3, minutes=30)).isoformat(),
}

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
    N.SESSION_TIME_SUGGESTED: {"default": {"suggested_after": "declined", **_SUGGESTED}},
    N.SESSION_SUGGESTION_REMINDER: {"default": dict(_SUGGESTED)},
    N.CREDITS_GRANTED: {"default": {"credit_count": "1"}},
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
#: Of those, the ones an admin reads rather than the mentor: the outbox gives
#: the admin's copy the admin's name and no side.
_READ_BY_ADMINS = frozenset({N.MENTOR_APPLICATION_RECEIVED})


#: Who reads each sample copy: the mentor, the mentee, or an admin.
_READERS = {
    "mentor": ("Ada Obi", "Ada"),
    "mentee": ("Mo Bello", "Mo"),
    "admins": ("Sam Eze", "Sam"),
}
#: A sample request's time left to answer, a little over the `hours` example.
_ANSWER_WITHIN = dt.timedelta(hours=18, minutes=30)


def _reader_side(email: Notification, variant: str) -> str:
    """Whose copy a variant is: its own side, the email's one audience, or the
    mentee's for an email that goes to either."""
    if variant in {"mentor", "mentee"}:
        return variant
    if email in _READ_BY_ADMINS:
        return "admins"
    if AUDIENCE.get(email) is Audience.MENTOR or email in _ABOUT_A_MENTOR:
        return "mentor"
    return "mentee"


def sample_context(email: Notification, variant: str, side: str | None = None) -> MessageContext:
    """The context the email's ``variant`` is previewed and checked against:
    its producer's sample row, shaped as the outbox shapes that email's, read
    by its recipient (``side`` reads it as the other party of a session)."""
    extras = dict(SAMPLES.get(email, {}).get(variant, {}))
    session = email in _ABOUT_A_SESSION
    reader = side or _reader_side(email, variant)
    name, first = _READERS[reader]
    return MessageContext(
        recipient_name=name,
        recipient_first_name=first,
        recipient_timezone="Africa/Lagos",
        mentor_name="Ada Obi" if session or email in _ABOUT_A_MENTOR else "",
        mentee_name="Mo Bello" if session else "",
        starts_at=_START if session else None,
        topic="Choosing a master's" if session else None,
        session_type_name="CV review" if session else None,
        venue="Google Meet" if session else None,
        # From the moment it is built, so the countdown reads as the example does.
        respond_by=dt.datetime.now(dt.UTC) + _ANSWER_WITHIN if email in _REQUESTS else None,
        duration_minutes=45 if session else None,
        session_id="00000000-0000-7000-8000-0000000000aa" if session else None,
        app_base_url="https://app.edufurther.example",
        extras=extras,
        notification=email,
        recipient_role=reader
        if reader != "admins" and (session or email in _ABOUT_A_MENTOR)
        else "",
    )


def _meets(value: Value, email: Notification, context: MessageContext) -> bool:
    if not value.needs:
        return True
    return any(
        context.starts_at is not None
        if need == "session"
        else email in _REQUESTS
        if need == "request"
        else need in context.extras
        for need in value.needs
    )


@functools.cache
def values_for(email: Notification, variant: str) -> tuple[str, ...]:
    """The `VALUES` a line of this variant may name: those its email is about
    and its sample fills. A value another email of its group owns does not
    fill here (`resolve`)."""
    context = sample_context(email, variant)
    found = []
    for value in VALUES:
        if not _meets(value, email, context):
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


#: A brace that is not part of a well-formed ``{name}``: ``{ name }``,
#: ``{first name}``, a lone ``{`` or ``}``. It would go out as written.
_STRAY = re.compile(r"\{[^{}]*\}?|\}")


def _suggest(name: str) -> str | None:
    """The catalogue name ``name`` most likely meant: the same ignoring case,
    else the closest spelling."""
    for known in sorted(VALUE_NAMES):
        if known.lower() == name.lower():
            return known
    close = difflib.get_close_matches(name.strip(), sorted(VALUE_NAMES), n=1, cutoff=0.8)
    return close[0] if close else None


def check_line(email: Notification, variant: str, line: EmailLine, text: str) -> list[LineError]:
    """Every reason ``text`` cannot be saved as this line, or none.

    The hard max counts the raw text, ``{name}``s included. A name a line may
    not say (a link, a card slot, a line itself) is not in `VALUES`, so it is as
    unknown as a misspelling, and so is a malformed ``{ name }``.
    """
    if not text.strip():
        return [LineError(line, LineErrorCode.EMPTY)]
    errors = []
    if len(text) > LINE_HARD_MAX[line]:
        errors.append(LineError(line, LineErrorCode.TOO_LONG))
    carried = values_for(email, variant)
    for name in dict.fromkeys(PLACEHOLDER.findall(text)):
        canonical = ALIASES.get(name, name)
        if canonical not in VALUE_NAMES:
            errors.append(LineError(line, LineErrorCode.UNKNOWN_VALUE, name, _suggest(name)))
        elif canonical not in carried:
            errors.append(LineError(line, LineErrorCode.VALUE_NOT_SENT, name))
    for stray in dict.fromkeys(_STRAY.findall(PLACEHOLDER.sub("", text))):
        inner = stray.strip("{}")
        errors.append(
            LineError(line, LineErrorCode.UNKNOWN_VALUE, stray, _suggest(inner) if inner else None)
        )
    return errors

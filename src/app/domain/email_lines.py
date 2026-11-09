"""Templates several emails share, and the lines the backend writes for them (decision 317).

**One Loops template, several emails.** An email (a `Notification` member) keeps
its own rules (who hears it, when, which switch governs it); several of them may
point at one Loops template id, which shows the sections for the right one by
`emailType` (and `recipientRole` where an email goes to both parties). Loops can
condition a section on a data variable, but not its subject, so a shared
template's **subject, heading and button come from here** as `emailSubject`,
`emailHeading`, `buttonLabel` and `buttonUrl`.

Lines may name any variable a template could ask for, as ``{name}``, and are
filled by the same resolvers (`domain.messages`). A template used by one email
only keeps its lines in Loops and asks for none of these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import EmailLine
from app.domain.message_emails import ONE_MESSAGE, TWO_FROM_ONE, TWO_FROM_TWO
from app.domain.notifications import Notification

__all__ = [
    "DEFAULT_VARIANT",
    "LINES",
    "LINE_HARD_MAX",
    "LINE_KEY_MAX_LENGTH",
    "LINE_NAMES",
    "OWNED_BY",
    "PLACEHOLDER",
    "TEMPLATE_GROUPS",
    "EmailLines",
    "case_of",
    "group_of",
    "lines_for",
    "variant_of",
]

N = Notification

#: The emails that share one Loops template, by the template's name. An email
#: belongs to one group at most.
TEMPLATE_GROUPS: dict[str, frozenset[Notification]] = {
    "session_confirmed": frozenset({N.SESSION_BOOKED, N.REQUEST_ACCEPTED}),
    "request_awaiting": frozenset(
        {N.SESSION_REQUESTED, N.MENTOR_RESPONSE_REMINDER, N.MENTEE_REQUEST_REMINDER}
    ),
    "request_closed": frozenset({N.REQUEST_DECLINED, N.REQUEST_WITHDRAWN}),
    "session_reminders": frozenset({N.SESSION_REMINDER, N.SESSION_LAST_REMINDER}),
    "suggested_time": frozenset({N.SESSION_TIME_SUGGESTED, N.SESSION_SUGGESTION_REMINDER}),
    "credits": frozenset({N.CREDITS_GRANTED, N.CREDITS_RENEWED, N.CREDITS_EXPIRING}),
    "mentor_status": frozenset({N.MENTOR_APPROVED, N.MENTOR_DECLINED, N.MENTOR_PAUSED_BY_ADMIN}),
    "messages": frozenset(
        {N.MESSAGE_UNREAD_FEW, N.MESSAGE_UNREAD_MANY, N.MESSAGE_IN_SESSION_WINDOW}
    ),
}

#: A variable only some emails of a group carry. In a shared template (one that
#: declares `emailType`) the others in a group with an owner fill it **empty**,
#: since their copy has it in a hidden section; anywhere else it still fails by
#: name. An owner set may span groups (`intervalTime`).
OWNED_BY: dict[str, frozenset[Notification]] = {
    "intervalTime": frozenset({N.CREDITS_EXPIRING, N.SESSION_REMINDER, N.SESSION_LAST_REMINDER}),
    "unreadCount": frozenset({N.MESSAGE_UNREAD_FEW, N.MESSAGE_UNREAD_MANY}),
    "senderFirstNames": frozenset({N.MESSAGE_UNREAD_MANY}),
    "senderFirstName": frozenset({N.MESSAGE_IN_SESSION_WINDOW}),
    "previewText": frozenset({N.MESSAGE_UNREAD_FEW, N.MESSAGE_IN_SESSION_WINDOW}),
    "repliesName": frozenset({N.MESSAGE_UNREAD_FEW}),
    "creditCount": frozenset({N.CREDITS_RENEWED, N.CREDITS_EXPIRING}),
    "suggestedAfter": frozenset({N.SESSION_TIME_SUGGESTED}),
    "reminderMessage": frozenset({N.MENTEE_REQUEST_REMINDER}),
}

#: The variables lines fill. A line may not name one of these (it would fill itself).
LINE_NAMES = frozenset({"emailSubject", "emailHeading", "buttonLabel", "buttonUrl"})

#: The most characters each line may hold, counted on the raw text with its
#: ``{name}``s (PRD 28, decision 318). A migration test pins the tables' checks.
LINE_HARD_MAX = {EmailLine.SUBJECT: 100, EmailLine.HEADING: 90, EmailLine.BUTTON: 32}
#: An override's email key and variant: a member's or a case's name.
LINE_KEY_MAX_LENGTH = 64
#: The variant an email's own lines (case ``""``) go by outside the code: in
#: the tables and the admin API (PRD 28).
DEFAULT_VARIANT = "default"

#: ``{name}`` in a line: a variable name as Loops allows them.
PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9_-]*)\}")


@dataclass(frozen=True, slots=True)
class EmailLines:
    """One email's lines. ``link`` names the variable whose value the button opens."""

    subject: str
    heading: str
    button: str
    link: str


_VIEW_SESSION = ("View session", "sessionUrl")
_REVIEW_REQUEST = ("Review request", "pendingRequestUrl")
_FIND_MENTOR = ("Find a mentor", "exploreUrl")
_SUGGESTED = ("Book the new time", "sessionUrl")


def _l(subject: str, heading: str, button: tuple[str, str]) -> EmailLines:
    return EmailLines(subject, heading, *button)


#: Every shared-template email's lines, keyed by a **case**: ``""`` for all,
#: ``"mentor"`` / ``"mentee"`` for the recipient's side, or a case the email
#: carries as ``line_case`` (the unread email's three).
LINES: dict[Notification, dict[str, EmailLines]] = {
    N.SESSION_BOOKED: {
        "mentor": _l(
            "New session booked with {menteeName}",
            "{menteeName} booked a session with you",
            _VIEW_SESSION,
        ),
        "mentee": _l(
            "Your session with {mentorName} is booked",
            "You're booked with {mentorName}",
            _VIEW_SESSION,
        ),
    },
    N.REQUEST_ACCEPTED: {
        "": _l(
            "{mentorName} accepted your request",
            "Your session with {mentorName} is confirmed",
            _VIEW_SESSION,
        )
    },
    N.SESSION_REQUESTED: {
        "": _l(
            "New session request from {menteeName}",
            "{menteeName} requested a session with you",
            _REVIEW_REQUEST,
        )
    },
    N.MENTOR_RESPONSE_REMINDER: {
        "": _l(
            "{menteeName} is waiting for your answer",
            "{hours} hours left to answer {menteeName}",
            _REVIEW_REQUEST,
        )
    },
    N.MENTEE_REQUEST_REMINDER: {
        "": _l(
            "{menteeName} sent you a reminder",
            "{menteeName} is waiting to hear from you",
            _REVIEW_REQUEST,
        )
    },
    N.REQUEST_DECLINED: {
        "": _l(
            "{mentorName} can't take your session request",
            "Your session request was declined",
            _FIND_MENTOR,
        )
    },
    N.REQUEST_WITHDRAWN: {
        "": _l(
            "{menteeName} withdrew their session request",
            "A session request was withdrawn",
            ("Open dashboard", "dashboardUrl"),
        )
    },
    N.SESSION_REMINDER: {
        "": _l(
            "Reminder: your session with {attendee}",
            "Your session is happening {intervalTime}",
            _VIEW_SESSION,
        )
    },
    N.SESSION_LAST_REMINDER: {
        "": _l(
            "Your session with {attendee} starts soon",
            "Your session starts in a few minutes",
            ("Go to session", "sessionUrl"),
        )
    },
    N.SESSION_TIME_SUGGESTED: {
        "": _l(
            "{mentorName} suggested a new time",
            "{mentorName} suggested another time for your session",
            _SUGGESTED,
        )
    },
    N.SESSION_SUGGESTION_REMINDER: {
        "": _l(
            "Your suggested time is held until {holdUntilTime}",
            "The time {mentorName} suggested is still free",
            _SUGGESTED,
        )
    },
    N.CREDITS_GRANTED: {
        "": _l(
            "Your session credits are here",
            "You have credits to book a session",
            _FIND_MENTOR,
        )
    },
    N.CREDITS_RENEWED: {
        "": _l(
            "Your credits for this month are ready",
            "You have new credits this month",
            _FIND_MENTOR,
        )
    },
    N.CREDITS_EXPIRING: {
        "": _l(
            "Your credits expire soon",
            "Use your credits before they expire",
            ("Book a session", "exploreUrl"),
        )
    },
    N.MENTOR_APPROVED: {
        "": _l(
            "You're approved as an EduFurther mentor",
            "Welcome aboard, {recipientFirstName}",
            ("Get started", "applicationUrl"),
        )
    },
    N.MENTOR_DECLINED: {
        "": _l(
            "An update on your mentor application",
            "Your mentor application wasn't approved",
            ("View application", "applicationUrl"),
        )
    },
    N.MENTOR_PAUSED_BY_ADMIN: {
        "": _l(
            "Your mentor profile is paused",
            "We've paused your mentor profile",
            ("Open dashboard", "dashboardUrl"),
        )
    },
    N.MESSAGE_UNREAD_FEW: {
        ONE_MESSAGE: _l(
            "{repliesName} sent you a message",
            "{repliesName} sent you a message",
            ("Reply in Messages", "chatUrl"),
        ),
        TWO_FROM_ONE: _l(
            "{unreadCount} new messages from {repliesName}",
            "{repliesName} sent you {unreadCount} messages",
            ("Reply in Messages", "chatUrl"),
        ),
        TWO_FROM_TWO: _l(
            "You have {unreadCount} unread messages on EduFurther",
            "You have {unreadCount} unread messages",
            ("Open Messages", "chatUrl"),
        ),
    },
    N.MESSAGE_UNREAD_MANY: {
        "": _l(
            "You have {unreadCount} unread messages on EduFurther",
            "{senderFirstNames} sent you {unreadCount} messages",
            ("Open Messages", "chatUrl"),
        )
    },
    N.MESSAGE_IN_SESSION_WINDOW: {
        "": _l(
            "{senderFirstName} sent you a message about your session",
            "{senderFirstName} sent you a message",
            ("Reply in Messages", "chatUrl"),
        )
    },
}


def variant_of(case: str) -> str:
    """A `LINES` case as the tables and the API name it."""
    return case or DEFAULT_VARIANT


def case_of(variant: str) -> str:
    """The `LINES` case a stored or requested variant names."""
    return "" if variant == DEFAULT_VARIANT else variant


def group_of(notification: Notification) -> frozenset[Notification]:
    """The emails sharing ``notification``'s template, or none."""
    for members in TEMPLATE_GROUPS.values():
        if notification in members:
            return members
    return frozenset()


def lines_for(notification: Notification, case: str, role: str) -> EmailLines | None:
    """The lines for this email: its carried case, else the recipient's side, else
    the email's own. ``None`` for an email outside every group."""
    found = LINES.get(notification)
    if found is None:
        return None
    for key in (*(k for k in (case, role) if k), ""):
        if key in found:
            return found[key]
    return None

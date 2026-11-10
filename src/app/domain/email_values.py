"""The values an email's lines may name, and what an empty one says (PRD 28, decision 319).

Below `domain.messages` and `domain.email_registry`, which both read it: the
resolvers' own fallbacks live here so the catalogue can name them, and the send
path reads `FALLBACKS` without importing the registry.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FALLBACKS",
    "NO_REASON_TITLE",
    "SESSION_TOPIC_FALLBACK",
    "VALUES",
    "VALUE_NAMES",
    "VENUE_FALLBACK",
    "Value",
]

#: **What is legitimately absent gets one defined value, not a failed send.**
#: The dev outbox showed the cost of refusing these (2026-10-03): most bookings
#: carry no topic and no decline carries a reason code, so `session_requested`
#: and `request_declined` failed and nobody was told. An unknown *name* is still
#: refused; these are only for values a real event often lacks. A decline,
#: withdrawal or cancellation has required a code since 2026-10-10 (decision
#: 237), so `NO_REASON_TITLE` now serves events written before that. Still
#: needed: an offer of another time ends a session with no code.
SESSION_TOPIC_FALLBACK = "Mentorship session"
VENUE_FALLBACK = "Online"
NO_REASON_TITLE = "No reason given"


@dataclass(frozen=True, slots=True)
class Value:
    """A value a line may name: what it means, an example, and what an empty one
    becomes in a line at send time (``None``: the line cannot go without it).

    ``needs`` is what the email must be about for the value to mean anything:
    ``"session"``, ``"request"``, or row keys any one of which it carries. A
    value with a fallback of its own (a topic, a venue) fills anywhere, so
    filling alone cannot say whether an email carries it.
    """

    name: str
    meaning: str
    example: str
    fallback: str | None
    needs: tuple[str, ...] = ()


_SESSION = ("session",)

#: The values a line may name, in the order the picker lists them. Card slots,
#: links and the lines themselves are not words a line says. A value whose
#: resolver already falls back (a topic, a venue, a reason) names that constant,
#: so the catalogue says what is really sent.
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
        SESSION_TOPIC_FALLBACK,
        _SESSION,
    ),
    Value("sessionName", "The session's name", "CV review", SESSION_TOPIC_FALLBACK, _SESSION),
    Value(
        "sessionDate",
        "The booking's date, in the reader's zone",
        "Fri 20 November 2026",
        "the day",
        _SESSION,
    ),
    Value(
        "sessionTime",
        "The booking's start time, in the reader's zone",
        "15:00",
        "the time",
        _SESSION,
    ),
    Value("sessionTimezone", "The reader's time zone", "Africa/Lagos", "your time zone", _SESSION),
    Value("sessionDuration", "Session length", "45 min", "the session", _SESSION),
    Value("location", "Where the booking happens", "Google Meet", VENUE_FALLBACK, _SESSION),
    Value("hours", "Whole hours left to answer the request", "18", "a few", ("request",)),
    Value("intervalTime", "How soon, in words", "Tomorrow", "soon"),
    Value(
        "reasonTitle",
        "Why it ended, in words",
        "Scheduling conflict",
        NO_REASON_TITLE,
        ("reason_code", "reason_text"),
    ),
    Value("suggestedDate", "The suggested time's date", "Sat 21 November 2026", "the day"),
    Value("suggestedTime", "The suggested start time", "16:00", "the time"),
    Value("holdUntilTime", "When the suggested time is released", "17:30", "soon"),
    Value("creditCount", "How many credits", "2", "your"),
    Value("repliesName", 'Who to reply to: a first name, or "them"', "Ada", "them"),
    Value("senderFirstName", "The sender's first name", "Ada", "someone"),
    Value("senderFirstNames", "The senders' first names", "Ada and Ben", "people"),
    Value("unreadCount", "How many messages are unread", "3", "new"),
)

VALUE_NAMES = {value.name for value in VALUES}

#: What an empty value says in a line at send time, by name (``None``: the
#: line cannot go without it). Read by the send path (`domain.messages`).
FALLBACKS: dict[str, str | None] = {value.name: value.fallback for value in VALUES}

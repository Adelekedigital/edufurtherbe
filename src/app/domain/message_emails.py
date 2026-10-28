"""The message emails (Messages PRD, decision 303): when they go and what they say.

**Two emails, three templates.** The unread email (M2) goes 15 minutes after a
send if the recipient has not read it, at most once an hour, and can be turned
off; it is the *few* template for one or two messages (each card cut to 140
characters at a word) and the *many* template for three or more (senders and
counts, no text). The session-window email (M3) goes at once, in full, for a
message sent within `chats.SESSION_WINDOW` of a session between the two; it is
always on and ignores the hour.

Pure: the caller loads the messages and this turns them into the values the
outbox row carries. Every key here is read by one resolver in
`domain.messages`: the lines every email of its kind has fail by name when
absent, and the cards and rows that may legitimately be missing (a second
card, a sixth sender) resolve empty. Times travel as ISO instants and are
rendered in the recipient's zone when the email is sent.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.chats import preview
from app.domain.notifications import Notification

__all__ = [
    "FEW_MAX",
    "MESSAGE_EMAILS",
    "SENDER_ROWS",
    "UNREAD_EMAILS",
    "UNREAD_EMAIL_DELAY",
    "UNREAD_EMAIL_INTERVAL",
    "UNREAD_EMAIL_LOOKBACK",
    "EmailedMessage",
    "held_until",
    "instant_email",
    "unread_email",
    "unread_kind",
]

#: How long a message waits unread before the unread email (founder, 2026-10-26).
UNREAD_EMAIL_DELAY = dt.timedelta(minutes=15)

#: At most one unread email per recipient in this long, across all chats. Any
#: message email starts it, the session-window one included, so the limit only
#: ever means fewer emails.
UNREAD_EMAIL_INTERVAL = dt.timedelta(hours=1)

#: How far back the unread email looks. A message older than this is never
#: gathered: an imported history, or one that waited while the switch was off,
#: is not a fresh message, and emailing it would flood a first email.
UNREAD_EMAIL_LOOKBACK = dt.timedelta(hours=24)

#: Up to this many unread messages get the few email, a card each; more get many.
FEW_MAX = 2

#: The many email's rows: one per sender, the most messages first. Senders past
#: this are counted in `other_senders` rather than dropped.
SENDER_ROWS = 5

#: The unread emails, which the recipient's switch governs.
UNREAD_EMAILS = frozenset({Notification.MESSAGE_UNREAD_FEW, Notification.MESSAGE_UNREAD_MANY})

#: Every message email: the drain loads their content from the message ids.
MESSAGE_EMAILS = UNREAD_EMAILS | {Notification.MESSAGE_IN_SESSION_WINDOW}

#: A sender with no name on their account.
NAMELESS = "Someone"


@dataclass(frozen=True, slots=True)
class EmailedMessage:
    """One message as an email shows it."""

    sender_id: UUID
    first_name: str | None
    last_name: str | None
    body: str | None
    file_name: str | None
    sent_at: dt.datetime


def held_until(last_emailed_at: dt.datetime | None, now: dt.datetime) -> dt.datetime | None:
    """When the next unread email may go, or ``None`` when it may go now.

    Rounded up to the whole second: QStash's not-before is whole seconds, so a
    job asked for a fraction earlier would arrive still held and loop.
    """
    if last_emailed_at is None:
        return None
    until = last_emailed_at + UNREAD_EMAIL_INTERVAL
    if until.microsecond:
        until = until.replace(microsecond=0) + dt.timedelta(seconds=1)
    return until if until > now else None


def _first(message: EmailedMessage) -> str:
    return message.first_name or message.last_name or NAMELESS


def _full(message: EmailedMessage) -> str:
    named = " ".join(part for part in (message.first_name, message.last_name) if part)
    return named or NAMELESS


def _initials(message: EmailedMessage) -> str:
    return "".join(part[0] for part in (message.first_name, message.last_name) if part).upper()


def _messages(count: int) -> str:
    return "1 message" if count == 1 else f"{count} messages"


def _card(number: int, message: EmailedMessage, *, whole: bool) -> dict[str, str]:
    """One message card: who, when, and the text (cut unless ``whole``)."""
    text = message.body or ""
    return {
        f"message{number}_name": _full(message),
        f"message{number}_initials": _initials(message),
        f"message{number}_sent_at": message.sent_at.isoformat(),
        f"message{number}_text": text if whole else (preview(text) if text else ""),
        f"message{number}_file_name": message.file_name or "",
    }


def _and(names: Sequence[str]) -> str:
    """ "Ada", "Ada and Ben", "Ada, Ben and Chi"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def unread_kind(count: int) -> Notification:
    """The unread email ``count`` messages make: few up to `FEW_MAX`, else many."""
    return Notification.MESSAGE_UNREAD_FEW if count <= FEW_MAX else Notification.MESSAGE_UNREAD_MANY


def unread_email(messages: Sequence[EmailedMessage]) -> dict[str, str]:
    """The unread email's values for ``messages``, oldest first.

    One or two are the few email; Q14's default covers two from two people:
    the subject counts them, one card each, and the button opens Messages.
    """
    if not messages:
        raise ValueError("an unread email needs a message")
    senders = {message.sender_id for message in messages}
    if unread_kind(len(messages)) is Notification.MESSAGE_UNREAD_MANY:
        return _many(messages)
    first = messages[0]
    values: dict[str, str] = {"preview_text": _preview(first)}
    for number, message in enumerate(messages, start=1):
        values |= _card(number, message, whole=False)
    if len(senders) == 1:
        name = _first(first)
        one = len(messages) == 1
        values |= {
            "email_subject": f"{name} sent you a message" if one else f"2 new messages from {name}",
            "email_heading": f"{name} sent you a message" if one else f"{name} sent you 2 messages",
            "button_label": "Reply in Messages",
            "reply_to": str(first.sender_id),
            "replies_name": name,
        }
    else:
        values |= {
            "email_subject": f"You have {len(messages)} unread messages on EduFurther",
            "email_heading": f"You have {len(messages)} unread messages",
            "button_label": "Open Messages",
            "replies_name": "them",
        }
    return values


def _preview(message: EmailedMessage) -> str:
    """The inbox's preview: the text cut, else the file's name."""
    return preview(message.body) if message.body else (message.file_name or "")


def _many(messages: Sequence[EmailedMessage]) -> dict[str, str]:
    """Senders and counts, no text: the most messages first, then the earliest."""
    counts = Counter(message.sender_id for message in messages)
    order: dict[UUID, EmailedMessage] = {}
    for message in messages:
        order.setdefault(message.sender_id, message)
    ranked = sorted(order.values(), key=lambda m: -counts[m.sender_id])
    values = {
        "unread_count": str(len(messages)),
        "sender_first_names": _and([_first(m) for m in ranked]),
    }
    for number, message in enumerate(ranked[:SENDER_ROWS], start=1):
        values |= {
            f"sender{number}_name": _full(message),
            f"sender{number}_initials": _initials(message),
            f"sender{number}_count": _messages(counts[message.sender_id]),
        }
    left = len(ranked) - SENDER_ROWS
    values["other_senders"] = f"and {left} more" if left > 0 else ""
    return values


def instant_email(message: EmailedMessage) -> dict[str, str]:
    """The session-window email's values: the message in full."""
    return {
        **_card(1, message, whole=True),
        "sender_first_name": _first(message),
        "preview_text": _preview(message),
        "reply_to": str(message.sender_id),
    }

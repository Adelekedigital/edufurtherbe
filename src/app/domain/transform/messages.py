"""Turning the legacy chats into conversations, messages and read marks
(02_FIELD_MAPPING.md §15-16; Messages PRD, Legacy import; decision 302).

    messageStarters  ->  conversations + conversation_participants
    messageThreads   ->  messages (+ a read mark from `seenRead`)

**One chat per pair, whatever Bubble held** (ADR 0010). Bubble could open two
starters between the same two people; `uq_conversations_pair` is unordered, so
they **merge** here, keyed on the pair, into one chat anchored on the earliest
starter (`legacy_bubble_id`), and every starter's messages land in it. A
message names its starter; the starter's pair names the chat.

**Who is the mentor is not decided here**: the export says who wrote first,
not who mentored. The loader reads it from the database (`chat_roles`).

WHAT EACH FIELD BECOMES
=======================
- ``messageContent`` → ``body`` (trimmed; blank is null, which an imported
  row may be: the body CHECKs exempt imported rows).
- ``sendBy`` → ``sender_id``, which must be one of the pair. ``receivedBy`` is
  dropped: the pair already says who the other person is.
- ``sentAt`` → ``sent_at``, falling back to ``Creation Date`` only when absent.
- ``dateLastEdited`` → ``edited_at``.
- ``softDelete`` (yes) → ``deleted_at`` at the row's ``Modified Date`` (the
  last time Bubble touched it, which a delete was), else its ``sent_at``.
- ``seenRead`` (yes) → the *other* person's ``last_read_at`` moves to at least
  that message's ``sent_at``; the newest seen message wins.
- ``messageRequestAccept`` (yes on any merged starter) → ``accepted``, else
  ``pending``. v1 reads no status (decision 301).
- ``lastMessageContent`` is not carried: the preview is built on read.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.domain.bubble import legacy_anchor
from app.domain.enums import ConversationStatus
from app.domain.transform.reviews import either, stamp, trimmed
from app.domain.transform.sessions import is_true

__all__ = [
    "ChatRow",
    "LegacyMessage",
    "MessagePlan",
    "Quarantined",
    "ReadMark",
    "chat_roles",
    "plan_messages",
]


@dataclass(frozen=True, slots=True)
class ChatRow:
    """One chat: one pair of people, from one or more starters."""

    #: The earliest starter's id: the chat's anchor.
    legacy_bubble_id: str
    #: The two people's Bubble ids, sorted, so a pair has one spelling.
    people: tuple[str, str]
    #: Every starter merged into this chat, the anchor first.
    starters: tuple[str, ...]
    #: Who opened the anchor starter (`created_by`).
    started_by: str
    status: ConversationStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    #: The newest message's `sent_at`, or None for a chat with none.
    last_message_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class LegacyMessage:
    """One message, ready to write."""

    legacy_bubble_id: str
    #: The chat's anchor, not the starter the message named.
    chat: str
    sender: str
    body: str | None
    sent_at: dt.datetime
    edited_at: dt.datetime | None
    deleted_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ReadMark:
    """How far one person had read one chat."""

    chat: str
    reader: str
    last_read_at: dt.datetime


@dataclass(frozen=True, slots=True)
class Quarantined:
    """A source row that cannot be written, and why. Reported, never coerced."""

    legacy_bubble_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class MessagePlan:
    """Everything the two exports turn into, before any of it is written."""

    chats: tuple[ChatRow, ...]
    messages: tuple[LegacyMessage, ...]
    read_marks: tuple[ReadMark, ...]
    #: Every anchor either export offered, starters first. Reconciliation
    #: needs a source list to be missing from (`reconcile.py`).
    source_anchors: tuple[str, ...] = ()
    quarantined: tuple[Quarantined, ...] = field(default=())

    def report(self) -> str:
        """The operator's account of one run (settled decision #45)."""
        merged = sum(len(chat.starters) - 1 for chat in self.chats)
        lines = [
            f"source rows: {len(self.source_anchors)}",
            f"chats to load: {len(self.chats)} ({merged} starter(s) merged into another)",
            f"messages to load: {len(self.messages)}",
            f"read marks: {len(self.read_marks)}",
            f"quarantined: {len(self.quarantined)}",
        ]
        lines += [f"  QUARANTINED {row.legacy_bubble_id}: {row.reason}" for row in self.quarantined]
        return "\n".join(lines)


def chat_roles(
    people: tuple[Any, Any], *, booked_mentor: Any | None, profile_holders: set[Any]
) -> tuple[Any, Any] | None:
    """``(mentor, mentee)`` for a legacy chat, or ``None`` when nothing says.

    The newest booking between the two decides, as it decides a chat's roles
    before its first message (`chat_store`); without one, the only one of the
    two with a mentor profile is the mentor. Anything else is not a guess.
    """
    first, second = people
    if booked_mentor in people:
        return (first, second) if booked_mentor == first else (second, first)
    holders = [person for person in people if person in profile_holders]
    if len(holders) == 1:
        return (first, second) if holders[0] == first else (second, first)
    return None


@dataclass(slots=True)
class _Starter:
    anchor: str
    people: tuple[str, str]
    sender: str
    accepted: bool
    created_at: dt.datetime
    updated_at: dt.datetime


def _starters(
    records: list[dict[str, Any]], timezone: dt.tzinfo, quarantined: list[Quarantined]
) -> list[_Starter]:
    seen: set[str] = set()
    starters: list[_Starter] = []
    for record in records:
        anchor = legacy_anchor(record)
        if not anchor:
            quarantined.append(Quarantined("<no id>", "the starter carries no unique id"))
            continue
        if anchor in seen:
            quarantined.append(Quarantined(anchor, "the export repeats this unique id"))
            continue
        seen.add(anchor)
        sender, receiver = trimmed(record.get("sendBy")), trimmed(record.get("receiveBy"))
        if not sender or not receiver:
            quarantined.append(Quarantined(anchor, "sendBy or receiveBy is absent"))
            continue
        if sender == receiver:
            quarantined.append(Quarantined(anchor, "sendBy and receiveBy are the same person"))
            continue
        created, reason = stamp(either(record, "created_at", "Creation Date"), timezone=timezone)
        if created is None:
            quarantined.append(Quarantined(anchor, f"Creation Date {reason}"))
            continue
        modified, reason = stamp(either(record, "modified_at", "Modified Date"), timezone=timezone)
        if modified is None and reason != "is absent":
            quarantined.append(Quarantined(anchor, f"Modified Date {reason}"))
            continue
        pair = (sender, receiver) if sender < receiver else (receiver, sender)
        starters.append(
            _Starter(
                anchor,
                pair,
                sender,
                is_true(record.get("messageRequestAccept")),
                created,
                modified or created,
            )
        )
    return starters


def _message(
    record: dict[str, Any],
    anchor: str,
    chat: str,
    people: tuple[str, str],
    timezone: dt.tzinfo,
) -> LegacyMessage | str:
    """The message, or the reason it has none."""
    sender = trimmed(record.get("sendBy"))
    if sender not in people:
        return "sendBy is not one of the two people in its chat"
    created, reason = stamp(either(record, "created_at", "Creation Date"), timezone=timezone)
    if created is None:
        return f"Creation Date {reason}"
    modified, reason = stamp(either(record, "modified_at", "Modified Date"), timezone=timezone)
    if modified is None and reason != "is absent":
        return f"Modified Date {reason}"
    sent, reason = stamp(record.get("sentAt"), timezone=timezone)
    if sent is None and reason != "is absent":
        return f"sentAt {reason}"
    edited, reason = stamp(record.get("dateLastEdited"), timezone=timezone)
    if edited is None and reason != "is absent":
        return f"dateLastEdited {reason}"
    sent_at = sent or created
    return LegacyMessage(
        legacy_bubble_id=anchor,
        chat=chat,
        sender=str(sender),
        body=trimmed(record.get("messageContent")),
        sent_at=sent_at,
        edited_at=edited,
        deleted_at=(modified or sent_at) if is_true(record.get("softDelete")) else None,
        created_at=created,
        updated_at=modified or created,
    )


def plan_messages(
    starters: list[dict[str, Any]], threads: list[dict[str, Any]], *, timezone: dt.tzinfo
) -> MessagePlan:
    """Every legacy chat and message, as rows or as reasons it has none.

    Pure: no I/O, no clock. The zone is the export's (`parse_timestamp`).
    """
    quarantined: list[Quarantined] = []
    source = [legacy_anchor(r) or "<no id>" for r in starters] + [
        legacy_anchor(r) or "<no id>" for r in threads
    ]
    by_pair: dict[tuple[str, str], list[_Starter]] = {}
    for starter in _starters(starters, timezone, quarantined):
        by_pair.setdefault(starter.people, []).append(starter)

    chat_of_starter: dict[str, tuple[str, tuple[str, str]]] = {}
    for people, group in by_pair.items():
        group.sort(key=lambda s: (s.created_at, s.anchor))
        for starter in group:
            chat_of_starter[starter.anchor] = (group[0].anchor, people)

    messages: list[LegacyMessage] = []
    seen: set[str] = set()
    reads: dict[tuple[str, str], dt.datetime] = {}
    for record in threads:
        anchor = legacy_anchor(record)
        if not anchor:
            quarantined.append(Quarantined("<no id>", "the message carries no unique id"))
            continue
        if anchor in seen:
            quarantined.append(Quarantined(anchor, "the export repeats this unique id"))
            continue
        seen.add(anchor)
        target = chat_of_starter.get(trimmed(record.get("messageStarter")) or "")
        if target is None:
            quarantined.append(Quarantined(anchor, "messageStarter is absent or not loaded"))
            continue
        chat, people = target
        made = _message(record, anchor, chat, people, timezone)
        if isinstance(made, str):
            quarantined.append(Quarantined(anchor, made))
            continue
        messages.append(made)
        if is_true(record.get("seenRead")):
            reader = people[1] if made.sender == people[0] else people[0]
            key = (chat, reader)
            reads[key] = max(reads.get(key, made.sent_at), made.sent_at)

    newest: dict[str, dt.datetime] = {}
    for message in messages:
        newest[message.chat] = max(newest.get(message.chat, message.sent_at), message.sent_at)
    chats = tuple(
        ChatRow(
            legacy_bubble_id=group[0].anchor,
            people=people,
            starters=tuple(s.anchor for s in group),
            started_by=group[0].sender,
            status=ConversationStatus.ACCEPTED
            if any(s.accepted for s in group)
            else ConversationStatus.PENDING,
            created_at=group[0].created_at,
            updated_at=max(s.updated_at for s in group),
            last_message_at=newest.get(group[0].anchor),
        )
        for people, group in by_pair.items()
    )
    return MessagePlan(
        chats=chats,
        messages=tuple(messages),
        read_marks=tuple(
            ReadMark(chat, reader, at) for (chat, reader), at in sorted(reads.items())
        ),
        source_anchors=tuple(source),
        quarantined=tuple(quarantined),
    )

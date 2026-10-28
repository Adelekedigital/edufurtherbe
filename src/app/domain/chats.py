"""Chats between a mentor and a mentee: the rules a message obeys (ADR 0010).

Named `chats` because `domain.messages` is already the email copy resolver.
Pure: what a message may hold. Who may write to whom is decided in the
statements that read and write, so it is checked in the query.
"""

from __future__ import annotations

__all__ = ["BODY_MAX_LENGTH"]

#: The most characters a message's text may hold, after trimming (Messages PRD;
#: SendMessage's limit, and Q10 applies it to the inbox too). Pinned to the
#: database's `ck_messages_body_length` by a test.
BODY_MAX_LENGTH = 2000

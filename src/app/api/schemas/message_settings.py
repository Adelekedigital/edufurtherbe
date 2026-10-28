"""The caller's message email switch (Messages PRD, decision 303)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["MessageSettingsRead", "MessageSettingsWrite"]

UNREAD_EMAILS_DESCRIPTION = (
    "Email me about unread messages: when messages are still unread, at the "
    "earliest 15 minutes after the first of them was sent, at most one email "
    "an hour. On unless switched off. A message sent from 15 minutes before to "
    "15 minutes after a session between you starts (one not cancelled) is "
    "emailed at once whatever this says, unless it is read first."
)


class MessageSettingsRead(BaseModel):
    """Which message emails you get."""

    unread_emails: bool = Field(description=UNREAD_EMAILS_DESCRIPTION)


class MessageSettingsWrite(BaseModel):
    """Switch the unread message emails on or off."""

    model_config = ConfigDict(extra="forbid", strict=True)

    unread_emails: bool = Field(description=UNREAD_EMAILS_DESCRIPTION)

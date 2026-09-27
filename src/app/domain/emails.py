"""How an email address is stored: trimmed and lowercased.

**One function, because the database holds everyone to it.** `users.email` is
plain `text` with `CHECK (email = lower(email))` and a unique index on live
rows, so an address normalised any other way is either refused by the CHECK —
a 500 — or slips past the index as a second account for the same person. It
was typed out four times before this (profile writes, referrals, the Bubble
transform, first sign-in); the copy that drifted would have been silent.
"""

from __future__ import annotations

__all__ = ["normalise_email"]


def normalise_email(value: str) -> str:
    """The stored form of an address."""
    return value.strip().lower()

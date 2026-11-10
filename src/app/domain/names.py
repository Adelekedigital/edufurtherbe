"""Splitting a full name into the first and last name the account stores.

Sign-up asks for one string (decision 258). **The first word is the first name
and the rest is the last name**: "Damilola Ade Ogunleye" is `Damilola` and
`Ade Ogunleye`. That keeps a greeting and the invite-link code to one word, and
keeps a surname particle with its surname ("van Beethoven"). "Mary Ann Smith"
splits the wrong way; the person corrects it with the two separate fields.

Pure: whether each part is a valid name is the boundary's rule, not this one.
"""

from __future__ import annotations

__all__ = ["NAMELESS", "addressed_by", "joined_name", "split_full_name"]

#: A person with no name on their account, wherever one is shown to another.
NAMELESS = "Someone"


def addressed_by(first_name: str | None, last_name: str | None) -> str:
    """What to call somebody in a notification: their first name, else their
    last, else `NAMELESS`. The one rule the emails and the pushes share."""
    return first_name or last_name or NAMELESS


def joined_name(first_name: str | None, last_name: str | None) -> str:
    """Whatever the row holds, joined with one space, or empty. `last_name` is
    nullable and empty for some migrated accounts, so an f-string would leave a
    trailing space."""
    return " ".join(part for part in (first_name, last_name) if part)


def split_full_name(value: str) -> tuple[str, str]:
    """The first word and the rest; `ValueError` for fewer than two words.

    Words are split on any run of whitespace, so the stored last name has
    single spaces between its words.
    """
    words = value.split()
    if len(words) < 2:
        raise ValueError("enter a first and a last name, separated by a space")
    return words[0], " ".join(words[1:])

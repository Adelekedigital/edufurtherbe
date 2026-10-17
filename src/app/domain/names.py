"""Splitting a full name into the first and last name the account stores.

Sign-up asks for one string (decision 258). **The first word is the first name
and the rest is the last name**: "Damilola Ade Ogunleye" is `Damilola` and
`Ade Ogunleye`. That keeps a greeting and the invite-link code to one word, and
keeps a surname particle with its surname ("van Beethoven"). "Mary Ann Smith"
splits the wrong way; the person corrects it with the two separate fields.

Pure: whether each part is a valid name is the boundary's rule, not this one.
"""

from __future__ import annotations

__all__ = ["split_full_name"]


def split_full_name(value: str) -> tuple[str, str] | None:
    """The first word and the rest, or `None` for fewer than two words.

    Words are split on any run of whitespace, so the stored last name has
    single spaces between its words.
    """
    words = value.split()
    if len(words) < 2:
        return None
    return words[0], " ".join(words[1:])

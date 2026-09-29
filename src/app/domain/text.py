"""What counts as a visible character in text a person typed.

**Unicode category C is never visible**: controls (Cc — a newline, a NUL),
formats (Cf — a zero-width space, the right-to-left override that makes
a name render with its letters reversed), private use (Co), surrogates (Cs)
and unassigned code points (Cn). One predicate, because the question "is this
character something a reader can see and a system can safely carry" has one
answer: an uploaded filename strips them, a name refuses them, and both ask
here.
"""

from __future__ import annotations

import unicodedata

__all__ = ["has_letter", "is_invisible", "visible_only"]


def is_invisible(character: str) -> bool:
    """Whether `character` is a control, format, private-use, surrogate or
    unassigned code point — anything in Unicode category C."""
    return unicodedata.category(character).startswith("C")


def visible_only(text: str) -> str:
    """`text` with every invisible character removed."""
    return "".join(character for character in text if not is_invisible(character))


def has_letter(text: str) -> bool:
    """Whether `text` holds at least one letter, in any script."""
    return any(unicodedata.category(character).startswith("L") for character in text)

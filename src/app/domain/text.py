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

__all__ = ["has_letter", "hidden_characters", "is_invisible", "visible_only"]

#: Zero-width non-joiner and joiner. Format characters, but **orthography** in
#: several scripts: Persian writes a ZWNJ inside words (Shift+Space on its
#: keyboard), and Sinhala needs a ZWJ to form conjuncts.
JOINERS = frozenset({chr(0x200C), chr(0x200D)})


def is_invisible(character: str) -> bool:
    """Whether `character` is a control, format, private-use, surrogate or
    unassigned code point — anything in Unicode category C."""
    return unicodedata.category(character).startswith("C")


def visible_only(text: str) -> str:
    """`text` with every invisible character removed."""
    return "".join(character for character in text if not is_invisible(character))


def _joins(before: str, after: str) -> bool:
    """Whether a joiner between these two characters is joining letters."""
    return all(unicodedata.category(side)[0] in "LM" for side in (before, after))


def hidden_characters(text: str) -> list[str]:
    """Every invisible character in `text` that is not orthography.

    A ZWNJ or ZWJ counts as orthography only **between two letters or combining
    marks** — so a leading, trailing, doubled or space-flanked joiner is hidden
    text, as is every other format character: a bidi override or isolate, a
    zero-width space. For a name, where a joiner is part of how it is spelled;
    a filename strips everything with `visible_only`.
    """
    hidden = []
    for index, character in enumerate(text):
        if not is_invisible(character):
            continue
        if (
            character in JOINERS
            and 0 < index < len(text) - 1
            and _joins(text[index - 1], text[index + 1])
        ):
            continue
        hidden.append(character)
    return hidden


def has_letter(text: str) -> bool:
    """Whether `text` holds at least one letter, in any script."""
    return any(unicodedata.category(character).startswith("L") for character in text)

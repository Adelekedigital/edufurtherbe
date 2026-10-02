"""What a typed search becomes before it reaches the database (#227).

Explore matches a query three ways, and ranks them in this order: the exact
word (full-text, stemmed for prose), a prefix of it ("harv" → Harvard), and a
near spelling ("Harvrd" → Harvard). The last two are built from **terms**: the
query's words, lowercased, with every character that is not a letter, a
digit or a combining mark dropped. That is what makes them safe to hand to `to_tsquery`, whose
syntax (`& | ! ( ) : * <->`) would otherwise be user-controlled, and it is
bound as a parameter besides.
"""

from __future__ import annotations

import re
import unicodedata

#: More words than this adds nothing a mentee means and only costs the query.
MAX_SEARCH_TERMS = 8
#: Longer than any real name, school or subject word.
MAX_TERM_LENGTH = 40
#: Below four characters a word has too few trigrams to tell a typo from
#: noise — "abc" is near-similar to half the directory.
MIN_FUZZY_CHARS = 4
#: The `strict_word_similarity` floor for the near-spelling tier, explicit
#: rather than `pg_trgm.strict_word_similarity_threshold`, which nothing in this
#: repository sets. Strict scoring compares whole words, so a word inside
#: another one is not a near spelling. Measured: "harvrd" scores 0.50 against
#: "harvard university", "scholarshp" 0.64 and "oxforrd" 0.67 against their
#: words, while "mark" in "nigeria denmark" and "hard" in "richard dawson" are
#: 0.30 (both 0.60 under plain `word_similarity`).
FUZZY_FLOOR = 0.45

#: A possessive "'s" (straight or curly apostrophe) closing a word.
_POSSESSIVE = re.compile(r"(?<=[^\W_])['\u2019]s\b")


def has_operators(q: str) -> bool:
    """Whether `q` asks for an exclusion or a phrase.

    `websearch_to_tsquery`'s operators: a `-` that starts a word negates it and a
    quote makes a phrase. A hyphen inside a word ("Smith-Jones") is neither;
    "inside" uses the same word characters as `search_terms`, after the same
    NFC, so a composed and a decomposed spelling read alike.

    Such a query is searched exactly and nothing else: the forgiving tiers read
    words without polarity, so ORing them in would put an excluded word back
    and loosen a phrase into separate words.
    """
    text = unicodedata.normalize("NFC", q)
    if '"' in text:
        return True
    return any(
        char == "-" and not _ends_word(text[:n]) and n + 1 < len(text) and _in_word(text[n + 1])
        for n, char in enumerate(text)
    )


def _ends_word(text: str) -> bool:
    """Whether `text` ends inside a word: its trailing marks sit on a letter or
    digit. Marks with no base are dropped by `search_terms`, so they are no word
    and a dash after them still starts one."""
    stripped = text.rstrip("".join(c for c in set(text) if unicodedata.category(c)[0] == "M"))
    return bool(stripped) and unicodedata.category(stripped[-1])[0] in "LN"


def _in_word(char: str) -> bool:
    """A letter, a digit, or a combining mark: Devanagari vowel signs and Arabic
    harakat are marks, and dropping them splits a word into its letters."""
    return unicodedata.category(char)[0] in "LNM"


def search_terms(q: str) -> list[str]:
    """The query's words, NFC-composed, lowercased, and stripped to letters,
    digits and the marks inside them. A possessive "'s" goes with its word,
    or the stray "s" would be required as a word of its own."""
    text = _POSSESSIVE.sub("", unicodedata.normalize("NFC", q.lower()))
    words = "".join(c if _in_word(c) else " " for c in text).split()
    # A word of marks alone yields no lexeme, and `:*` with no operand is a
    # `to_tsquery` syntax error.
    words = [w for w in words if any(unicodedata.category(c)[0] in "LN" for c in w)]
    return [w[:MAX_TERM_LENGTH] for w in words][:MAX_SEARCH_TERMS]


def search_groups(q: str) -> list[list[str]]:
    """The query's terms, split on `or` the way `websearch_to_tsquery` reads it.

    An `or` between two words is a disjunction; at either end it is a word. Each
    group must match in full and any group will do, so the forgiving tiers keep
    the boolean shape the exact tier already has.
    """
    terms = search_terms(q)
    groups: list[list[str]] = [[]]
    for n, term in enumerate(terms):
        if term == "or" and groups[-1] and n < len(terms) - 1:
            groups.append([])
        else:
            groups[-1].append(term)
    return [group for group in groups if group]


def prefix_terms(terms: list[str]) -> list[str]:
    """Each term as a `to_tsquery` prefix pattern; the store ANDs them."""
    return [f"{term}:*" for term in terms]


def fuzzy_terms(terms: list[str]) -> tuple[list[str], list[str]]:
    """`(near, present)`: the terms long enough to be matched as near spellings,
    and the shorter ones, which have too few trigrams to tell a typo from noise
    and so must be present as a prefix instead."""
    near = [term for term in terms if len(term) >= MIN_FUZZY_CHARS]
    return near, [term for term in terms if len(term) < MIN_FUZZY_CHARS]

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

#: `websearch_to_tsquery`'s operators: a `-` that starts a word negates it and a
#: quote makes a phrase. A hyphen inside a word ("Smith-Jones") is neither.
_OPERATOR = re.compile(r'(?:^|[^\w])-\w|"')


def has_operators(q: str) -> bool:
    """Whether `q` asks for an exclusion or a phrase.

    Such a query is searched exactly and nothing else: the forgiving tiers read
    words without polarity, so ORing them in would put an excluded word back
    and loosen a phrase into separate words.
    """
    return _OPERATOR.search(q) is not None


def _in_word(char: str) -> bool:
    """A letter, a digit, or a combining mark: Devanagari vowel signs and Arabic
    harakat are marks, and dropping them splits a word into its letters."""
    return unicodedata.category(char)[0] in "LNM"


def search_terms(q: str) -> list[str]:
    """The query's words, NFC-composed, lowercased, and stripped to letters,
    digits and the marks inside them."""
    text = unicodedata.normalize("NFC", q.lower())
    words = "".join(c if _in_word(c) else " " for c in text).split()
    # A word of marks alone yields no lexeme, and `:*` with no operand is a
    # `to_tsquery` syntax error.
    words = [w for w in words if any(unicodedata.category(c)[0] in "LN" for c in w)]
    return [w[:MAX_TERM_LENGTH] for w in words][:MAX_SEARCH_TERMS]


def prefix_terms(terms: list[str]) -> list[str]:
    """Each term as a `to_tsquery` prefix pattern; the store ANDs them."""
    return [f"{term}:*" for term in terms]


def fuzzy_terms(terms: list[str]) -> list[str]:
    """The terms long enough to be matched as near spellings, each of which must
    be near. A shorter word has too few trigrams to tell a typo from noise, so
    the near tier leaves it to the exact and prefix tiers."""
    return [term for term in terms if len(term) >= MIN_FUZZY_CHARS]

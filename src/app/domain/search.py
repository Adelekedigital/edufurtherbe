"""What a typed search becomes before it reaches the database (#227).

Explore matches a query three ways, and ranks them in this order: the exact
word (full-text, stemmed for prose), a prefix of it ("harv" → Harvard), and a
near spelling ("Harvrd" → Harvard). The last two are built from **terms**: the
query's words, lowercased, with every character that is not a letter or a
digit dropped. That is what makes them safe to hand to `to_tsquery`, whose
syntax (`& | ! ( ) : * <->`) would otherwise be user-controlled, and it is
bound as a parameter besides.
"""

from __future__ import annotations

import re

#: More words than this adds nothing a mentee means and only costs the query.
MAX_SEARCH_TERMS = 8
#: Longer than any real name, school or subject word.
MAX_TERM_LENGTH = 40
#: Below four characters a query has too few trigrams to tell a typo from
#: noise — "abc" is near-similar to half the directory.
MIN_FUZZY_CHARS = 4
#: The `word_similarity` floor for the near-spelling tier, explicit rather than
#: `pg_trgm.word_similarity_threshold`, which nothing in this repository sets.
#: Measured: "harvrd" scores 0.71 against "harvard university" and
#: "scholarshp" 0.73 against "scholarship", while an unrelated word
#: ("zebrafish") stays under 0.3 against a typical card.
FUZZY_FLOOR = 0.5

#: A letter or digit in any script; underscore is the one `\w` character that
#: is neither, and `to_tsquery` would split on it.
_TERM = re.compile(r"[^\W_]+")


def search_terms(q: str) -> list[str]:
    """The query's words, lowercased and stripped to letters and digits."""
    terms = [t[:MAX_TERM_LENGTH] for t in _TERM.findall(q.lower())]
    return terms[:MAX_SEARCH_TERMS]


def prefix_query(terms: list[str]) -> str | None:
    """A `to_tsquery` that matches every term as a prefix, or None for no terms."""
    return " & ".join(f"{term}:*" for term in terms) if terms else None


def fuzzy_text(terms: list[str]) -> str | None:
    """The text compared for near spellings, or None when too short to be useful."""
    text = " ".join(terms)
    return text if len(text) >= MIN_FUZZY_CHARS else None

"""The search input rules: what a typed query becomes before it reaches SQL."""

from __future__ import annotations

from app.domain.search import (
    MAX_SEARCH_TERMS,
    MAX_TERM_LENGTH,
    MIN_FUZZY_CHARS,
    fuzzy_text,
    has_operators,
    prefix_query,
    search_terms,
)


def test_terms_are_lowercased_words_and_nothing_else() -> None:
    assert search_terms("  Harv  UNIV ") == ["harv", "univ"]


def test_query_syntax_never_survives_into_a_term() -> None:
    assert search_terms("a & | ! ( :* <-> 'x'") == ["a", "x"]
    assert search_terms("&&&") == []


def test_letters_from_any_script_are_kept() -> None:
    assert search_terms("Ñandú São") == ["ñandú", "são"]


def test_terms_are_bounded_in_number_and_length() -> None:
    terms = search_terms(" ".join(["word"] * 50) + " " + "y" * 500)
    assert len(terms) == MAX_SEARCH_TERMS
    assert all(len(t) <= MAX_TERM_LENGTH for t in search_terms("z" * 500))


def test_the_prefix_query_ands_every_term_as_a_prefix() -> None:
    assert prefix_query(["harv", "univ"]) == "harv:* & univ:*"


def test_no_terms_means_no_prefix_query() -> None:
    assert prefix_query([]) is None


def test_a_short_query_is_not_matched_fuzzily() -> None:
    """Three letters are too few trigrams to tell a typo from noise."""
    assert fuzzy_text(["abc"]) is None
    assert fuzzy_text(["abcd"]) == "abcd"
    assert len("abcd") == MIN_FUZZY_CHARS


def test_fuzzy_text_joins_the_terms() -> None:
    assert fuzzy_text(["harvrd", "univ"]) == "harvrd univ"


def test_a_negation_or_phrase_is_an_operator_query() -> None:
    assert has_operators("Lovelace -Harvard")
    assert has_operators("-Harvard")
    assert has_operators('"harvard university"')


def test_a_hyphenated_word_is_not_a_negation() -> None:
    assert not has_operators("Smith-Jones")
    assert not has_operators("harv univ")

"""The search input rules: what a typed query becomes before it reaches SQL."""

from __future__ import annotations

from app.domain.search import (
    MAX_SEARCH_TERMS,
    MAX_TERM_LENGTH,
    MIN_FUZZY_CHARS,
    fuzzy_terms,
    has_operators,
    prefix_terms,
    search_groups,
    search_terms,
)


def test_terms_are_lowercased_words_and_nothing_else() -> None:
    assert search_terms("  Harv  UNIV ") == ["harv", "univ"]


def test_query_syntax_never_survives_into_a_term() -> None:
    assert search_terms("a & | ! ( :* <-> 'x'") == ["a", "x"]
    assert search_terms("&&&") == []


def test_letters_from_any_script_are_kept() -> None:
    assert search_terms("Ñandú São") == ["ñandú", "são"]
    # Combining marks are part of the word: the vowel signs in Devanagari and
    # the harakat in vocalised Arabic.
    assert search_terms("राम शर्मा") == ["राम", "शर्मा"]
    assert search_terms("مُحَمَّد") == ["مُحَمَّد"]


def test_a_decomposed_accent_is_composed() -> None:
    assert search_terms("São") == ["são"]


def test_terms_are_bounded_in_number_and_length() -> None:
    terms = search_terms(" ".join(["word"] * 50) + " " + "y" * 500)
    assert len(terms) == MAX_SEARCH_TERMS
    assert all(len(t) <= MAX_TERM_LENGTH for t in search_terms("z" * 500))


def test_every_term_becomes_a_prefix_pattern() -> None:
    assert prefix_terms(["harv", "univ"]) == ["harv:*", "univ:*"]
    assert prefix_terms([]) == []


def test_a_short_word_is_not_matched_fuzzily() -> None:
    """Three letters are too few trigrams to tell a typo from noise."""
    assert fuzzy_terms(["abc"]) == ([], ["abc"])
    assert fuzzy_terms(["abcd"]) == (["abcd"], [])
    assert len("abcd") == MIN_FUZZY_CHARS


def test_short_words_must_be_present_beside_near_ones() -> None:
    assert fuzzy_terms(["mit", "harvrd", "at"]) == (["harvrd"], ["mit", "at"])


def test_a_negation_or_phrase_is_an_operator_query() -> None:
    assert has_operators("Lovelace -Harvard")
    assert has_operators("-Harvard")
    assert has_operators('"harvard university"')


def test_a_negation_after_punctuation_is_still_a_negation() -> None:
    assert has_operators("lovelace (-harvard")


def test_a_hyphenated_word_is_not_a_negation() -> None:
    assert not has_operators("Smith-Jones")
    assert not has_operators("harv univ")


def test_a_mark_with_no_letter_is_not_a_term() -> None:
    """A lone combining mark yields no lexeme, and `:*` with no operand is a
    `to_tsquery` syntax error (Codex on #328)."""
    assert search_terms("́") == []
    assert search_terms("́́ ok") == ["ok"]
    assert search_terms("á") == ["á"]


def test_or_between_words_splits_the_query_into_groups() -> None:
    assert search_groups("Harvrd OR Oxford") == [["harvrd"], ["oxford"]]
    assert search_groups("mit harvrd or oxford uni") == [["mit", "harvrd"], ["oxford", "uni"]]


def test_or_at_either_end_is_a_word_as_websearch_reads_it() -> None:
    assert search_groups("or harvard") == [["or", "harvard"]]
    assert search_groups("harvard or") == [["harvard", "or"]]
    assert search_groups("") == []


def test_a_decomposed_hyphenated_word_is_not_a_negation() -> None:
    """The same word composed or not must read the same (Codex on #328)."""
    assert not has_operators("Á-Be")
    assert not has_operators("Á-Be")
    assert has_operators("x -Á")


def test_a_possessive_belongs_to_its_word() -> None:
    """A stray "s" would be required as a word of its own (Codex on #328)."""
    assert search_terms("Harvrd's") == ["harvrd"]
    assert search_terms("Harvrd\u2019s course") == ["harvrd", "course"]
    assert search_terms("it's") == ["it"]


def test_a_lone_mark_before_a_dash_does_not_hide_a_negation() -> None:
    """A mark with no letter under it is not a word, so the dash still starts
    one (Codex on #331)."""
    assert has_operators("́-Harvard")
    assert has_operators("x ́́-Harvard")
    assert not has_operators("Á́-Be")

"""Splitting a full name into a first and a last name (decision 258)."""

from __future__ import annotations

import pytest

from app.domain.names import split_full_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Grace Hopper", ("Grace", "Hopper")),
        ("Damilola Ade Ogunleye", ("Damilola", "Ade Ogunleye")),
        ("Ludwig van Beethoven", ("Ludwig", "van Beethoven")),
        ("  Grace \t Brewster   Hopper ", ("Grace", "Brewster Hopper")),
        ("Ada\u00a0Lovelace", ("Ada", "Lovelace")),
    ],
)
def test_the_first_word_is_the_first_name(value: str, expected: tuple[str, str]) -> None:
    assert split_full_name(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "Grace", "  Grace  "])
def test_fewer_than_two_words_is_no_split(value: str) -> None:
    assert split_full_name(value) is None

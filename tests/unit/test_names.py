"""Splitting a full name into a first and a last name (decision 258)."""

from __future__ import annotations

import pytest

from app.api.schemas.profile import UserProfileWrite
from app.domain.names import split_full_name
from app.domain.text import hidden_characters


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
def test_fewer_than_two_words_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="first and a last name"):
        split_full_name(value)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"], ids=["line", "paragraph"])
def test_a_unicode_line_break_is_hidden(separator: str) -> None:
    """Category Zl and Zp are line breaks, not spaces: a name refuses them like `\\n`."""
    assert hidden_characters(f"Ada{separator}Lovelace") == [separator]


def test_the_writer_receives_the_two_names_and_not_the_full_name() -> None:
    body = UserProfileWrite.model_validate({"full_name": "Grace Brewster Hopper"})

    assert body.model_dump(exclude_unset=True) == {
        "first_name": "Grace",
        "last_name": "Brewster Hopper",
    }

"""Public profile handles (owner, 2026-11-14): one rule for every account.

The frontend reads `/mentors/{handle}` as an id or a slug, so a slug must fit
its handle pattern and never look like a UUID.
"""

from __future__ import annotations

from itertools import islice

import pytest

from app.domain.handles import RESERVED_HANDLES
from app.domain.slugs import (
    HANDLE_PATTERN,
    MAX_SLUG_LENGTH,
    candidates,
    is_servable,
    looks_like_id,
    name_slug,
    placeholder_slug,
    strip_accents,
)
from app.domain.transform.identity import fold_slug

FE_HANDLE = HANDLE_PATTERN


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("Chloé", "Ńwosu", "chloe-nwosu"),
        ("Bánkólé", "Wright", "bankole-wright"),
        ("  Ada ", " Lovelace  ", "ada-lovelace"),
        ("Mary-Jane", "O'Neil", "mary-jane-o-neil"),
        ("Ada", None, "ada"),
        (None, "Okafor", "okafor"),
        ("Søren", "Kierkegaard", "s-ren-kierkegaard"),
    ],
)
def test_a_name_becomes_a_readable_handle(
    first: str | None, last: str | None, expected: str
) -> None:
    slug = name_slug(first, last)
    assert slug == expected
    assert FE_HANDLE.match(slug)


@pytest.mark.parametrize(
    ("first", "last"), [(None, None), ("", "  "), ("山田", "太郎"), ("---", "!!")]
)
def test_a_name_with_nothing_usable_gives_none(first: str | None, last: str | None) -> None:
    assert name_slug(first, last) is None


def test_a_long_name_is_cut_at_a_hyphen_within_the_limit() -> None:
    slug = name_slug("Oluwadamilare Babatunde Adekunle", "Oluwaseun-Abiodun Fashola-Williams")
    assert slug is not None
    assert len(slug) <= MAX_SLUG_LENGTH
    assert not slug.endswith("-")
    assert "oluwadamilare-babatunde-adekunle-oluwaseun-abiodun-fashola".startswith(slug)


def test_a_single_overlong_word_is_cut_at_the_limit() -> None:
    slug = name_slug("a" * 80, None)
    assert slug == "a" * MAX_SLUG_LENGTH


def test_a_uuid_shaped_name_is_not_a_handle() -> None:
    """`/mentors/{handle}` tries an id first; a UUID-shaped slug would shadow one."""
    assert name_slug("123e4567-e89b-12d3-a456", "426614174000") is None


def test_a_placeholder_fits_the_handle_and_names_no_one() -> None:
    slug = placeholder_slug("k3f9x2ab")
    assert slug == "user-k3f9x2ab"
    assert FE_HANDLE.match(slug)


@pytest.mark.parametrize("token", ["K3F9X2AB", "k3f9x2a", "k3f9x2ab9", "k3f9-2ab"])
def test_a_placeholder_token_is_eight_lowercase_base36(token: str) -> None:
    with pytest.raises(ValueError):
        placeholder_slug(token)


def test_clashes_count_up_from_two() -> None:
    assert list(islice(candidates("ada-lovelace"), 3)) == [
        "ada-lovelace",
        "ada-lovelace-2",
        "ada-lovelace-3",
    ]


def test_a_suffix_never_pushes_a_slug_past_the_limit() -> None:
    base = "a" * MAX_SLUG_LENGTH
    second = list(islice(candidates(base), 2))[1]
    assert len(second) == MAX_SLUG_LENGTH
    assert second.endswith("-2")


@pytest.mark.parametrize("word", sorted(RESERVED_HANDLES))
def test_a_reserved_word_is_never_a_handle_on_its_own(word: str) -> None:
    first = next(candidates(word))
    assert first == f"{word}-2"


def test_the_frontends_twelve_route_words_are_reserved() -> None:
    assert {
        "me", "new", "edit", "admin", "settings", "api", "explore",
        "invite", "mentors", "mentees", "null", "undefined",
    } <= RESERVED_HANDLES  # fmt: skip


@pytest.mark.parametrize(
    ("slug", "servable"),
    [
        ("chloe-nwosu", True),
        ("-odd", False),
        ("admin", False),
        ("123e4567-e89b-12d3-a456-426614174000", False),
    ],
)
def test_a_handle_is_servable_only_if_the_router_can_reach_it(slug: str, servable: bool) -> None:
    assert is_servable(slug) is servable


def test_strip_accents_refuses_what_has_no_ascii_form() -> None:
    """The import's rule (decision 311): an imported handle is never altered
    beyond its accents."""
    assert strip_accents("Bánkólé-Wright") == "Bankole-Wright"
    assert strip_accents("Søren") is None


def test_the_import_still_folds_exactly_as_before() -> None:
    assert fold_slug("Bánkólé-Wright") == "bankole-wright"
    assert fold_slug("Søren") is None


@pytest.mark.parametrize(
    "handle",
    [
        "123e4567-e89b-12d3-a456-426614174000",
        "123e4567e89b12d3a456426614174000",
        "deadbeefcafebeadfaceabbadadfeed1",
    ],
)
def test_anything_the_resolver_reads_as_an_id_is_refused(handle: str) -> None:
    """The resolver asks `UUID(handle)`, which takes 32 hex digits with or
    without hyphens; a handle it would read as an id could never be reached."""
    assert looks_like_id(handle)
    assert not is_servable(handle)


def test_a_hex_only_name_never_becomes_an_id_shaped_handle() -> None:
    assert name_slug("Dead Beef Cafe Bead", "Face Abba Dada Feed") is None

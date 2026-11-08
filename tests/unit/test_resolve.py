"""Legacy display names to reference ids (``app.domain.resolve``)."""

from uuid import uuid4

import pytest

from app.domain.resolve import COUNTRY_ALIASES, LANGUAGE_ALIASES, resolve_names


@pytest.mark.parametrize(
    ("raw", "seeded", "aliases"),
    [
        ("Syria", "Syrian Arab Republic", COUNTRY_ALIASES),
        ("TANZANIA", "Tanzania, United Republic of", COUNTRY_ALIASES),
        ("tanzania ", "Tanzania, United Republic of", COUNTRY_ALIASES),
        ("South Korea", "Korea, Republic of", COUNTRY_ALIASES),
        ("Turkey", "Türkiye", COUNTRY_ALIASES),
        ("Spanish; Castilian", "Spanish", LANGUAGE_ALIASES),
        ("Mandarin", "Mandarin Chinese", LANGUAGE_ALIASES),
        ("Swahili", "Swahili (macrolanguage)", LANGUAGE_ALIASES),
    ],
)
def test_production_names_resolve_through_an_alias(
    raw: str, seeded: str, aliases: dict[str, str]
) -> None:
    """Decision 316. Alias keys match in any case, as names already do."""
    identifier = uuid4()
    result = resolve_names({raw}, {seeded: identifier}, aliases)
    assert result.unresolved == ()
    assert result[raw] == identifier


def test_a_name_with_no_alias_stays_unresolved() -> None:
    result = resolve_names({"Biomedical Science"}, {"Nigeria": uuid4()}, COUNTRY_ALIASES)
    assert result.unresolved == ("Biomedical Science",)

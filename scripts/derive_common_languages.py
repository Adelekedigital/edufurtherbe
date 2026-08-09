"""Derive the `is_common` language set from CLDR, and print it as a literal.

    uv run python scripts/derive_common_languages.py

**The migration never fetches anything.** A migration that reached the network
would fail on a runner with no egress, would produce a different schema
depending on when it ran, and could not be replayed. So this script prints the
literal the migration holds, and a test asserts the two still agree — drift from
CLDR fails the suite rather than rotting quietly.

WHY CLDR, AND WHY THE "MODERN" TIER
===================================
Unicode's Common Locale Data Repository is what consumer products actually use
to decide which languages they ship; ISO 639-3, which this table is keyed on, is
a completeness registry of 7,078 entries and is not a picker. CLDR's `modern`
coverage tier is the set with full locale data — 430 locales, which collapse to
about 100 base languages.

Measured: 101 of our 7,078 rows are covered.

WHAT IS DELIBERATELY EXCLUDED
=============================
``und`` — "undetermined". Not a language.

``pcm`` — Nigerian Pidgin. **CLDR includes it at modern tier; leaving it out of
the default list is this project's choice, not the standard's.** It stays fully
searchable, and the 639-3 decision that put it in the table at all still stands.

WHAT IS DELIBERATELY KEPT OUT OF SCOPE
======================================
The other 6,977 rows stay. Replacing the table with the common set would delete
Efik, Ibibio, Tiv, Kanuri, Idoma, Urhobo, Nupe, Gbagyi, Esan, Ebira and Jukun —
every one a Nigerian language, on a platform for African students. The flag
costs one boolean; the deletion costs a coverage gap aimed at our own users.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams

CLDR_URL = (
    "https://raw.githubusercontent.com/unicode-org/cldr-json/main/"
    "cldr-json/cldr-core/coverageLevels.json"
)

#: Not languages, or excluded by decision. See the module docstring.
EXCLUDED = frozenset({"und", "pcm"})


def modern_base_languages() -> set[str]:
    """CLDR's `modern` tier, collapsed from locales to base language subtags.

    `ar-AE` and `ar-EG` are both Arabic; a picker wants the language, not the
    thirty regional variants of it.
    """
    response = httpx.get(CLDR_URL, timeout=60, follow_redirects=True)
    response.raise_for_status()
    levels = json.loads(response.content)["effectiveCoverageLevels"]
    return {locale.split("-")[0] for locale, level in levels.items() if level == "modern"}


async def main() -> int:
    configure_streams()
    modern = modern_base_languages() - EXCLUDED
    print(f"CLDR modern, collapsed and filtered: {len(modern)} base languages")

    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT code_639_3, code_639_1, display_name FROM languages ORDER BY code_639_3"
                )
            )
            held = [(row.code_639_3, row.code_639_1, row.display_name) for row in rows]
    finally:
        await engine.dispose()

    # Matched on either code: nine CLDR entries are three-letter-only and would
    # be missed by a `code_639_1` join alone.
    matched = [
        iso3 for iso3, iso1, _ in held if iso3 in modern or (iso1 is not None and iso1 in modern)
    ]
    unmatched = sorted(modern - set(matched) - {iso1 for _, iso1, _ in held if iso1})

    print(f"matched against `languages`: {len(matched)}")
    if unmatched:
        print(f"in CLDR but not held: {len(unmatched)} -> {', '.join(unmatched)}")

    if not matched:
        print("\nno matches — refusing to print a seed that would flag nothing")
        return EXIT_REFUSED

    print("\nCOMMON_LANGUAGES = (")
    for index in range(0, len(matched), 8):
        chunk = ", ".join(f'"{code}"' for code in sorted(matched)[index : index + 8])
        print(f"    {chunk},")
    print(")")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

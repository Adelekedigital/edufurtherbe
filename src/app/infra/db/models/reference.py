"""Reference data: ISO 3166-1 countries and ISO 639-3 languages.

Both are foreign-key targets rather than free-standing lookups — six columns
across identity and profiles reference ``countries(code)``, and
``user_languages`` references ``languages(code_639_3)`` — so both must be
complete before M1 loads a single row.

**Natural keys, not surrogate ids.** ``persistence-patterns`` says every table
carries its own surrogate ``id``, and that rule is overridden here. The ISO code
is exactly the value every foreign key stores, so a surrogate would mean each FK
holds a UUID that must be joined back to recover the code the caller already had.
The trade the generic rule is making — uniformity, nothing to remember per table
— does not pay when the natural key is externally standardised, immutable in
practice, and already the thing being referenced.

**No ``legacy_bubble_id``.** The guardrail applies to *migrated* tables. These
are not migrated from Bubble; they are reference data seeded from a published
standard, and a column that would be null on all 7,327 rows is not an anchor.
"""

from sqlalchemy import CHAR, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class Country(TimestampMixin, Base):
    """A country, keyed by its ISO 3166-1 alpha-2 code."""

    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    code_alpha3: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)


class Language(TimestampMixin, Base):
    """A living language, keyed by its ISO 639-3 alpha-3 code.

    ``code_639_1`` is null for roughly 98% of rows — the two-letter set covers
    only 174 of these — and that is the entire reason this table is keyed on
    639-3. The two-letter set omits Nigerian Pidgin (``pcm``) altogether, which
    for this platform's market is not an acceptable gap. The column is retained
    for ``hreflang`` and browser locale hints, where a two-letter code is
    required and its absence simply means no hint.
    """

    __tablename__ = "languages"

    code_639_3: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    code_639_1: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

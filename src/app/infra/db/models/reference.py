"""Reference data: ISO 3166-1 countries and ISO 639-3 languages.

Both are foreign-key targets rather than free-standing lookups — several columns
across identity and profiles reference ``countries``, and ``user_languages``
references ``languages`` — so both must be complete before M1 loads a single row.

**Surrogate primary keys, like every other table (ADR 0015).** An earlier version
of this file keyed both on their ISO code and argued the case at length: the code
is externally standardised, stable, and already the value a foreign key would
store, so a surrogate meant holding a UUID that had to be joined back to recover
what the caller already had.

That argument was accepted, and it was reversed — not because it was wrong on its
own terms, but because a rule with exceptions costs every future reader the memory
of the exceptions. Two tables out of sixty-six behaving differently is precisely
the kind of thing that gets rediscovered, re-argued, and re-decided differently in
M3. Consistency across the codebase is worth more than the join it saves here, and
at this data volume the join is free.

**The ISO code is still the human-facing key.** It keeps its own `UNIQUE`
constraint, it is still `NOT NULL`, and every lookup by code works exactly as
before. What changed is only what a *referencing* row stores.

**Reference ids are not stable across environments.** The seed omits ``id`` and
lets the column default generate one, so `NG` has a different id in every
database. Nothing may depend on a literal reference id — resolve by code.
Deriving ids from the code instead was considered and rejected: a surrogate
computed from the natural key moves whenever ISO retires or reassigns a code,
which is the volatility a surrogate exists to absorb.

**No ``legacy_bubble_id``.** That guardrail applies to *migrated* tables. These
are seeded from a published standard, and a column null on all 7,327 rows is not
an anchor.
"""

import uuid

from sqlalchemy import CHAR, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class Country(TimestampMixin, Base):
    """A country, identified by a surrogate id and keyed for humans by its
    ISO 3166-1 alpha-2 code."""

    __tablename__ = "countries"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # `unique=True`, not `index=True` — the constraint creates its own index, and
    # adding a second would be pure write overhead. This was the primary key
    # until ADR 0015; it stays unique and not-null, so nothing about looking a
    # country up by code changed.
    code: Mapped[str] = mapped_column(CHAR(2), nullable=False, unique=True)
    code_alpha3: Mapped[str] = mapped_column(CHAR(3), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)


class Language(TimestampMixin, Base):
    """A living language, keyed for humans by its ISO 639-3 alpha-3 code.

    ``code_639_1`` is null for roughly 98% of rows — the two-letter set covers
    only 174 of these — and that is the entire reason this table uses 639-3. The
    two-letter set omits Nigerian Pidgin (``pcm``) altogether, which for this
    platform's market is not an acceptable gap. The column is retained for
    ``hreflang`` and browser locale hints, where a two-letter code is required and
    its absence simply means no hint.
    """

    __tablename__ = "languages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    code_639_3: Mapped[str] = mapped_column(CHAR(3), nullable=False, unique=True)
    # Unique where present. PostgreSQL treats nulls as distinct, so the ~98% of
    # rows with no two-letter code are unaffected.
    code_639_1: Mapped[str | None] = mapped_column(CHAR(2), nullable=True, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

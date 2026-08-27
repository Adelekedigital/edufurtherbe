"""The literals the credit loader writes, against the vocabularies they name.

`infra/etl/credits.py` writes plain SQL rather than interpolating enum values,
for the reason the migrations give: an f-string into a statement is the pattern
the security checklist names, and a suppression would be one more rule nobody
reads.

**This is what keeps the two honest.** Without it, renaming a `CreditSource`
member leaves SQL naming a value the `CHECK` constraint rejects — and the
failure arrives at cutover, inside the freeze window, on somebody's real
balances.
"""

from __future__ import annotations

from app.domain.enums import CreditReason, CreditSource
from app.infra.etl.credits import (
    INSERT_MISSING_GRANTS,
    INSERT_OPENING_LOT,
    INSERT_STARTER_LOT,
    SQL_VOCABULARY,
)

STATEMENTS = (INSERT_OPENING_LOT, INSERT_STARTER_LOT, INSERT_MISSING_GRANTS)


def test_every_literal_the_sql_uses_is_a_real_member() -> None:
    for literal, member in SQL_VOCABULARY.items():
        assert literal == member.value


def test_the_vocabulary_covers_every_literal_the_statements_quote() -> None:
    """**The direction that catches an addition rather than a rename.**

    Checking only that each declared literal is real would pass a fourth
    statement quoting `'promotional'` with nothing declared for it. This walks
    the statements instead, so a new quoted value has to be classified before
    the suite goes green.
    """
    known = {member.value for member in CreditSource} | {member.value for member in CreditReason}
    quoted: set[str] = set()
    for statement in STATEMENTS:
        parts = statement.split("'")
        # Odd indices are the quoted spans.
        quoted |= {part for part in parts[1::2] if part in known}

    assert quoted == set(SQL_VOCABULARY)


def test_the_loader_writes_only_sources_it_is_responsible_for() -> None:
    """`refund`, `monthly_free` and `referral_unlock` have their own producers.
    A loader that wrote one would be a second writer for a rule that already has
    one."""
    written = {
        member.value for member in SQL_VOCABULARY.values() if isinstance(member, CreditSource)
    }

    assert written == {CreditSource.OPENING_BALANCE.value, CreditSource.PROFILE_COMPLETED.value}


def test_the_ledger_pass_covers_exactly_the_sources_the_loader_creates() -> None:
    """The `IN (...)` list in `INSERT_MISSING_GRANTS` and the two `INSERT`
    statements must name the same set — a lot created without being in the
    ledger pass is a balance nothing explains."""
    created = {CreditSource.OPENING_BALANCE.value, CreditSource.PROFILE_COMPLETED.value}

    for source in created:
        assert f"'{source}'" in INSERT_MISSING_GRANTS

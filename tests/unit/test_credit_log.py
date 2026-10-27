"""The ledger kind table is total (decision 299): every reason and every lot
source has a kind, so a new member cannot ship and reach the log unnamed."""

from __future__ import annotations

from app.domain.credit_log import ENTRY_KINDS, FILTER_KINDS, GRANT_KINDS, REASON_KINDS
from app.domain.enums import CreditReason, CreditSource


def test_every_reason_but_a_grant_names_its_own_kind() -> None:
    assert set(REASON_KINDS) == set(CreditReason) - {CreditReason.GRANT}


def test_every_lot_source_names_a_grant_kind() -> None:
    assert set(GRANT_KINDS) == set(CreditSource)


def test_every_named_kind_is_published_and_every_published_kind_is_reachable() -> None:
    named = set(REASON_KINDS.values()) | set(GRANT_KINDS.values())

    assert named == set(ENTRY_KINDS)


def test_the_chips_name_published_kinds_and_all_is_every_kind() -> None:
    for kinds in FILTER_KINDS.values():
        assert set(kinds) <= set(ENTRY_KINDS)
    assert set(FILTER_KINDS["all"]) == set(ENTRY_KINDS)
    # Every kind but `expired` has a chip of its own.
    chipped = {kind for chip, kinds in FILTER_KINDS.items() if chip != "all" for kind in kinds}
    assert chipped == set(ENTRY_KINDS) - {"expired"}

"""Which part of the card a credit sits in: monthly or bonus (decision 232, #344)."""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

from app.domain.credits import SOURCE_BUCKET, BonusGroup, HeldLot, bucket_of, split_buckets
from app.domain.enums import CreditSource

OCT_END = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
NOV_END = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)


def lot(
    source: CreditSource, remaining: int = 1, expires_at: dt.datetime | None = OCT_END
) -> HeldLot:
    return HeldLot(lot_id=uuid4(), source=source, remaining=remaining, expires_at=expires_at)


def split(held: list[HeldLot], replaces: dict[UUID, UUID] | None = None, extra: list[HeldLot] = ()):  # type: ignore[assignment]
    sources = {item.lot_id: item.source for item in [*held, *extra]}
    return split_buckets(held, sources=sources, replaces=replaces or {}, ceiling=3)


def test_every_credit_source_has_a_bucket_rule() -> None:
    """A new source must be placed deliberately, or this fails."""
    assert set(SOURCE_BUCKET) == set(CreditSource)


def test_the_monthly_grant_and_a_migrated_balance_are_monthly() -> None:
    assert SOURCE_BUCKET[CreditSource.MONTHLY_FREE] == "monthly"
    assert SOURCE_BUCKET[CreditSource.OPENING_BALANCE] == "monthly"


def test_starter_invite_and_support_grants_are_bonus() -> None:
    for source in (
        CreditSource.PROFILE_COMPLETED,
        CreditSource.REFERRAL_UNLOCK,
        CreditSource.ADMIN_GRANT,
    ):
        assert SOURCE_BUCKET[source] == "bonus"


def test_a_refund_follows_the_credit_it_replaces() -> None:
    monthly = lot(CreditSource.MONTHLY_FREE, remaining=0)
    starter = lot(CreditSource.PROFILE_COMPLETED, remaining=0, expires_at=None)
    back_monthly = lot(CreditSource.REFUND)
    back_starter = lot(CreditSource.REFUND, expires_at=None)
    sources = {item.lot_id: item.source for item in (monthly, starter, back_monthly, back_starter)}
    replaces = {back_monthly.lot_id: monthly.lot_id, back_starter.lot_id: starter.lot_id}

    assert bucket_of(back_monthly.lot_id, sources, replaces) == "monthly"
    assert bucket_of(back_starter.lot_id, sources, replaces) == "bonus"


def test_a_refund_of_a_refund_follows_the_chain() -> None:
    monthly = lot(CreditSource.MONTHLY_FREE, remaining=0)
    first = lot(CreditSource.REFUND, remaining=0)
    second = lot(CreditSource.REFUND)
    sources = {item.lot_id: item.source for item in (monthly, first, second)}
    replaces = {second.lot_id: first.lot_id, first.lot_id: monthly.lot_id}

    assert bucket_of(second.lot_id, sources, replaces) == "monthly"


def test_a_refund_with_no_known_origin_is_bonus() -> None:
    orphan = lot(CreditSource.REFUND)

    assert bucket_of(orphan.lot_id, {orphan.lot_id: orphan.source}, {}) == "bonus"


def test_a_looping_chain_ends_as_bonus_rather_than_hanging() -> None:
    a, b = lot(CreditSource.REFUND), lot(CreditSource.REFUND)
    sources = {a.lot_id: a.source, b.lot_id: b.source}

    assert bucket_of(a.lot_id, sources, {a.lot_id: b.lot_id, b.lot_id: a.lot_id}) == "bonus"


def test_a_starter_only_user_has_one_never_expiring_bonus_credit() -> None:
    monthly, bonus = split([lot(CreditSource.PROFILE_COMPLETED, expires_at=None)])

    assert (monthly.balance, monthly.ceiling, monthly.expires_at) == (0, 3, None)
    assert bonus.balance == 1
    assert bonus.groups == (BonusGroup(count=1, expires_at=None),)


def test_a_full_month_and_the_starter_read_three_of_three_plus_one() -> None:
    monthly, bonus = split(
        [
            lot(CreditSource.MONTHLY_FREE, remaining=3),
            lot(CreditSource.PROFILE_COMPLETED, expires_at=None),
        ]
    )

    assert (monthly.balance, monthly.ceiling, monthly.expires_at) == (3, 3, OCT_END)
    assert bonus.balance == 1


def test_bonus_groups_run_soonest_first_and_never_last() -> None:
    _, bonus = split(
        [
            lot(CreditSource.PROFILE_COMPLETED, expires_at=None),
            lot(CreditSource.ADMIN_GRANT, remaining=2, expires_at=NOV_END),
            lot(CreditSource.REFERRAL_UNLOCK, expires_at=OCT_END),
            lot(CreditSource.ADMIN_GRANT, expires_at=OCT_END),
        ]
    )

    assert bonus.groups == (
        BonusGroup(count=2, expires_at=OCT_END),
        BonusGroup(count=2, expires_at=NOV_END),
        BonusGroup(count=1, expires_at=None),
    )
    assert bonus.balance == 5


def test_monthly_expiry_is_the_soonest_held_monthly_lot() -> None:
    monthly, _ = split(
        [
            lot(CreditSource.MONTHLY_FREE, expires_at=NOV_END),
            lot(CreditSource.OPENING_BALANCE, expires_at=OCT_END),
        ]
    )

    assert monthly.expires_at == OCT_END
    assert monthly.balance == 2


def test_the_two_parts_add_up_to_everything_held() -> None:
    held = [
        lot(CreditSource.MONTHLY_FREE, remaining=2),
        lot(CreditSource.PROFILE_COMPLETED, expires_at=None),
        lot(CreditSource.ADMIN_GRANT, remaining=3),
        lot(CreditSource.REFUND),
    ]

    monthly, bonus = split(held)

    assert monthly.balance + bonus.balance == sum(item.remaining for item in held)


def test_a_monthly_part_that_never_expires_has_no_expiry_rather_than_failing() -> None:
    """A migrated opening balance carried with no expiry is still monthly."""
    monthly, _ = split([lot(CreditSource.OPENING_BALANCE, remaining=2, expires_at=None)])

    assert (monthly.balance, monthly.expires_at) == (2, None)

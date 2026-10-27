"""What one credit ledger row means to an admin reading it (decision 299).

The ledger records a `CreditReason` and, through its lot, a `CreditSource`. An
admin needs one word for what happened, so this is the one table from that pair
to a kind: the history and the platform log both read it, as the `CASE` the store
builds from it, so the two cannot name the same row differently.

**A grant is named by where its lot came from**; every other reason names itself.
A refund lot's own grant row is written with its refund reason, never `grant`,
so `CreditSource.REFUND` under `grant` is not produced by any writer; it is
named `refund` so the table is total rather than leaving a row unnamed.
"""

from __future__ import annotations

from typing import Literal, get_args

from app.domain.enums import CreditReason, CreditSource

__all__ = [
    "ENTRY_KINDS",
    "FILTER_KINDS",
    "GRANT_KINDS",
    "REASON_KINDS",
    "REFUND_CAUSES",
    "CreditEntryKind",
    "CreditLogFilter",
    "RefundReason",
]

#: What happened, in one word. `expired` is a lot reaching its date, written by
#: the sweep and by an account's deletion.
CreditEntryKind = Literal[
    "booking",
    "refund",
    "expired",
    "monthly_free",
    "profile_completed",
    "admin_grant",
    "referral_unlock",
    "opening_balance",
]
ENTRY_KINDS: tuple[CreditEntryKind, ...] = get_args(CreditEntryKind)

#: Why a refund was paid. A cancellation is split by who called it off, by the
#: rule the session read's `cancelled_by` uses; `canceled` is anybody else (the
#: system, or an admin acting as one). `request_unfulfilled` is a request that
#: never became a session: declined, withdrawn or expired.
RefundReason = Literal[
    "mentor_canceled",
    "mentee_canceled",
    "canceled",
    "mentor_no_show",
    "request_unfulfilled",
]
REFUND_CAUSES: tuple[RefundReason, ...] = get_args(RefundReason)

#: The kind of every reason but `grant`.
REASON_KINDS: dict[CreditReason, CreditEntryKind] = {
    CreditReason.SESSION_BOOKED: "booking",
    CreditReason.SESSION_CANCELLED_REFUND: "refund",
    CreditReason.SESSION_NO_SHOW_REFUND: "refund",
    CreditReason.REQUEST_UNFULFILLED: "refund",
    CreditReason.LOT_EXPIRED: "expired",
}

#: The kind of a `grant`, by its lot's source.
GRANT_KINDS: dict[CreditSource, CreditEntryKind] = {
    CreditSource.MONTHLY_FREE: "monthly_free",
    CreditSource.PROFILE_COMPLETED: "profile_completed",
    CreditSource.ADMIN_GRANT: "admin_grant",
    CreditSource.REFERRAL_UNLOCK: "referral_unlock",
    CreditSource.OPENING_BALANCE: "opening_balance",
    CreditSource.REFUND: "refund",
}

#: The credit log's chips. `all` is every kind, `expired` included, which no
#: other chip shows.
CreditLogFilter = Literal["all", "admin", "bookings", "refunds", "monthly"]

FILTER_KINDS: dict[CreditLogFilter, tuple[CreditEntryKind, ...]] = {
    "all": ENTRY_KINDS,
    "admin": ("admin_grant",),
    "bookings": ("booking",),
    "refunds": ("refund",),
    "monthly": ("monthly_free", "profile_completed", "referral_unlock", "opening_balance"),
}

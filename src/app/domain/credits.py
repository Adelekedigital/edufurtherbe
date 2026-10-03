"""What a credit is, where one comes from, and when it dies.

Pure, and in ``domain`` because every line is a product rule. Four things grant
credits — onboarding, a qualifying invite, the monthly job and the migration —
and if each decided its own expiry the four would drift. The rule is written
once here and read from there, which is non-negotiable #8 applied before the
second copy exists rather than after.

WHERE THE VOCABULARIES LIVE
===========================
``CreditSource`` and ``CreditReason`` are declared in ``domain/enums.py``
and re-exported here. Not a preference: the registry-partition test asserts
``vars(domain.enums)`` against the two ``infra`` registries **in both
directions**, so a vocabulary defined in this module would register as
"registered but not declared". The rules below are what this module owns.

WHAT IS NOT IN THEM
===================
**No ``purchase`` and no ``promotional``.** The canonical DDL declares seven
sources; settled decision #21 names this exact enum as its cautionary example,
because `credit_source` "contains `purchase` while payments are out of scope by
decision #8". A vocabulary is not a wish list: PostgreSQL cannot remove a value
from a native enum at all, and while #100 moved these to ``text`` + ``CHECK`` —
which *is* freely alterable — a shipped member still invites a writer, and the
reason to defer is that nothing writes one.

``admin_grant`` was deferred on the same argument and now ships, because support
needs a way to make somebody whole and direct SQL against production is the wrong
tool for it.

**No ``session_no_show_forfeit``.** It reads as a transaction and is not one.
The credit left the balance when the session was booked; a mentee who does not
turn up simply gets nothing back, and the absence of a row is the whole record.
A member for it would eventually be written by somebody reading the name as an
instruction, and the balance would be debited twice for one session.

THE RESET IS A PLATFORM BOUNDARY, NOT A LOCAL ONE
=================================================
``end_of_month`` is UTC. Users span Lagos, Toronto and Berlin, so a per-user
local midnight would mean the monthly job has no single moment to run and two
users in one household could see different reset dates. The dashboard card shows
one date — ``Next reset date: 1st, Sep 2026`` — and one date is only truthful if
the boundary is shared.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from app.core.config import Settings
from app.domain.enums import CreditReason, CreditSource, CreditState

__all__ = [
    "NON_EXPIRING",
    "SOURCE_BUCKET",
    "BonusCredits",
    "BonusGroup",
    "Bucket",
    "CreditLadder",
    "CreditReason",
    "CreditSource",
    "CreditState",
    "HeldLot",
    "MonthlyCredits",
    "allowance_for",
    "bucket_of",
    "credit_ladder",
    "end_of_month",
    "expiry_for",
    "refund_expiry",
    "split_buckets",
    "state_for",
]


#: Sources whose lots outlive the monthly reset. One today; every purchased lot
#: joins it when payments land, which is why this is a set rather than an
#: ``is PROFILE_COMPLETED`` comparison at the two call sites.
NON_EXPIRING: frozenset[CreditSource] = frozenset({CreditSource.PROFILE_COMPLETED})


@dataclass(frozen=True, slots=True)
class CreditLadder:
    """How much each rung of the earning ladder is worth.

    **One object rather than three loose numbers**, because the three are only
    meaningful together: `steady_state` is a relationship between two of them,
    and a caller holding one without the others cannot compute it.
    """

    #: Finishing onboarding.
    starter: int
    #: Added by the first qualifying invite, on top of the starter.
    unlock: int
    #: Granted on the 1st to every unlocked mentee, and the cap on what a
    #: migrated user may carry in.
    monthly: int

    @property
    def steady_state(self) -> int:
        """What the card's progress bar divides by — **the ceiling, not the grant**.

        The non-expiring starter plus the monthly grant, which is where a
        settled, unlocked mentee sits on the 1st. The bar is a position marker
        whose filled segment is the balance, so the denominator has to be a
        *fixed* ceiling: with `max(monthly, balance)` the denominator tracked the
        numerator and `balance == allowance` for every balance at or above the
        monthly grant — a four-segment bar full at four, then a three-segment bar
        still full at three. **The bar did not move when a credit was spent.**
        Found by code review.
        """
        return self.starter + self.monthly


def credit_ladder(settings: Settings) -> CreditLadder:
    """The configured ladder, from settings the caller already holds.

    Configuration because the economics are still moving, not because the rules
    are: what each grant *means* — the starter never expiring, the unlock being
    once-only, the monthly one needing a referral — stays here in `domain`, and
    only the sizes come from it.

    **``settings`` is required, and an earlier version called `get_settings()`
    itself.** That was wrong twice over. `deps._configured` states the rule:
    *"`get_settings()` is an `lru_cache` over the environment, so calling it
    directly means an app built with explicit settings — which is how every test
    builds one — runs on whatever the process happens to hold instead."* The
    ladder was the one thing in the credit model that ignored `app.state.settings`,
    so there was no way to configure it per app.

    And it made `domain` read the environment and open a dotenv file, which is
    what CLAUDE.md's layout rule means by *"pure Python, no I/O"*.
    `check_layers` permits the import direction, so no gate would have seen it.

    Taking the object instead puts the decision in the composition roots — the
    request dependency, the scripts, the ETL — which is where every other piece
    of configuration in this codebase is already resolved.
    """
    return CreditLadder(
        starter=settings.credit_starter_grant,
        unlock=settings.credit_referral_unlock_grant,
        monthly=settings.credit_monthly_allowance,
    )


def end_of_month(moment: dt.datetime) -> dt.datetime:
    """The instant a lot granted in ``moment``'s month stops being spendable.

    **Exclusive — the 1st of the next month at midnight UTC**, not the last
    instant of this one. The card promises ``Next reset date: 1st, Sep 2026``,
    so an August lot has to survive all of 31 August and die as September opens.
    An inclusive end-of-August would kill it a day before the date on screen.

    Computed by stepping to the 1st and adding a month rather than by adding 31
    days, which is wrong in February and wrong again in a leap year.

    **Normalised to UTC first, and that is not decoration.** The module docstring
    says the reset is a platform boundary; the implementation used to read
    ``moment.year`` and ``moment.month`` from whatever zone the caller passed.
    ``users.timezone`` is a real column and Lagos is the fixtures' default, so
    a caller standing at ``2026-09-01T00:30+01:00`` — which is
    ``2026-08-31T23:30Z`` — got October rather than September: a whole extra
    month of credit for anyone east of UTC granted in the last hour, and a month
    short for anyone west of it in the first. Found by code review before any
    caller passed a local time.

    A naive datetime is refused rather than assumed to be UTC. Guessing is how
    the offset gets silently discarded, which is the same reason every timestamp
    column here is ``timestamptz``.
    """
    if moment.tzinfo is None:
        raise ValueError("end_of_month needs an aware datetime; a naive one hides its offset")

    moment = moment.astimezone(dt.UTC)
    year, month = moment.year, moment.month
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return dt.datetime(year, month, 1, tzinfo=dt.UTC)


def expiry_for(source: CreditSource, *, now: dt.datetime) -> dt.datetime | None:
    """When a newly granted lot of ``source`` should die, or ``None`` for never.

    ``REFUND`` is refused rather than answered. A refund's expiry is a property
    of the lot it replaces, so answering here would mean guessing — and the
    guess that looks harmless, end of month, is exactly the one that turns a
    purchased credit perishable the day payments land. :func:`refund_expiry` is
    the function that has the fact it needs.
    """
    if source is CreditSource.REFUND:
        raise ValueError("a refund's expiry comes from the lot it replaces — use refund_expiry()")
    return None if source in NON_EXPIRING else end_of_month(now)


def refund_expiry(original: dt.datetime | None, *, now: dt.datetime) -> dt.datetime | None:
    """When a refunded credit dies, given the expiry of the lot that paid.

    **Inherits the semantics, never the date.** Both refund triggers are settled
    after the session has run, so a no-show swept on 2 September refunds a
    credit spent in August: handing back the original date would hand back
    something already dead, which is a refund in name only.

    A lot that never expired refunds one that never expires. Today that is the
    starter; once payments land it is every purchased credit, and D7 is blunt
    about why that has to hold — expiring something somebody bought is a
    chargeback and, in several jurisdictions, unlawful. Writing the rule this
    way now means the payments work adds a source rather than revisiting this.
    """
    return None if original is None else end_of_month(now)


def state_for(balance: int, ladder: CreditLadder) -> CreditState:
    """Which band a balance falls in.

    **The top band is open-ended** — at or above the steady state. A late refund
    can land a credit after the monthly grant, so a table written as ``4..5``
    leaves six unclassified, and an unclassified balance renders as an empty card
    rather than a full one.

    **The bands follow the ladder, and an earlier version hardcoded them.** With
    ``0 / 1 / <= 3 / > 3`` written as literals they were exactly right while the
    monthly grant was three and the steady state four — and silently wrong the
    moment either moved. At a monthly grant of two the steady state is three,
    `allowance_for` returns a full card at three, and `ON_TRACK` became
    unreachable in normal operation: the two derived values on the same card
    disagreed about the same balance.

    Zero and one stay absolute. They are not proportions — *nothing left* and
    *one left* mean the same thing whatever the grant is, and a "low" band that
    scaled would call three credits low on a generous ladder.

    A negative balance is refused rather than banded. The database makes it
    unrepresentable — ``quantity_remaining >= 0`` — so reaching here means a
    real defect upstream, and quietly calling it ``exhausted`` would let it
    render as an ordinary empty card and never be noticed.
    """
    if balance < 0:
        raise ValueError(f"a balance cannot be negative: {balance}")
    if balance == 0:
        return CreditState.EXHAUSTED
    if balance == 1:
        return CreditState.LOW
    if balance < ladder.steady_state:
        return CreditState.MODERATE
    return CreditState.ON_TRACK


def allowance_for(balance: int, ladder: CreditLadder) -> int:
    """What the card divides by — the denominator its progress bar draws.

    ``max(ladder.steady_state, balance)`` — the **ceiling**, not the monthly grant.

    A fixed denominator is what makes the bar move. Dividing by
    the monthly grant made ``balance == allowance`` for every balance at or
    above three, so a mentee at the steady state of four saw a full four-segment
    bar, spent a credit, and saw a full *three*-segment bar. The number changed
    and the picture did not.

    It still raises rather than clamps, because **the bar cannot draw more
    segments than it has**: migrated users arrive at five, and a refund landing
    after the monthly grant can push a balance past four. Clamping would read
    "5 credits left" beside a bar with four positions.

    The server publishes ``balance`` and ``allowance`` and never a percentage —
    the client draws, and a third representation of one fact is the first to
    drift.
    """
    return max(ladder.steady_state, balance)


# --------------------------------------------------------------------------
# Monthly and bonus credits (decision 232)
# --------------------------------------------------------------------------

#: The two parts of the card. The bar draws only the monthly part, so a full
#: month reads "3 of 3" rather than "3 of 4" (#344).
Bucket = Literal["monthly", "bonus"]

#: Which part a lot of each source sits in. **One mapping**; a test pins every
#: `CreditSource` member to it.
#:
#: ``REFUND`` maps to ``None`` because a refund has no bucket of its own: it
#: goes back to the bucket of the credit it replaces (owner, 2026-10-03), found
#: by :func:`bucket_of`. ``OPENING_BALANCE`` is monthly because it stands in for
#: the cutover month's grant, which is why it is capped at the grant.
SOURCE_BUCKET: Mapping[CreditSource, Bucket | None] = MappingProxyType(
    {
        CreditSource.MONTHLY_FREE: "monthly",
        CreditSource.OPENING_BALANCE: "monthly",
        CreditSource.PROFILE_COMPLETED: "bonus",
        CreditSource.REFERRAL_UNLOCK: "bonus",
        CreditSource.ADMIN_GRANT: "bonus",
        CreditSource.REFUND: None,
    }
)


@dataclass(frozen=True, slots=True)
class HeldLot:
    """A lot with spendable credit in it, as the split needs it."""

    lot_id: UUID
    source: CreditSource
    remaining: int
    expires_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class MonthlyCredits:
    """The monthly part of the card."""

    balance: int
    #: The monthly grant. A late refund can briefly put ``balance`` above it.
    ceiling: int
    #: The soonest instant a held monthly credit stops being spendable.
    expires_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class BonusGroup:
    """Bonus credits that die at the same instant, or never."""

    count: int
    expires_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class BonusCredits:
    """Every credit that is not the monthly grant."""

    balance: int
    #: Soonest expiry first, never-expiring last.
    groups: tuple[BonusGroup, ...]


def bucket_of(
    lot_id: UUID, sources: Mapping[UUID, CreditSource], replaces: Mapping[UUID, UUID]
) -> Bucket:
    """Which part of the card a lot belongs to.

    ``sources`` is every lot the user has had, spent or not, because a refund's
    origin is usually spent. ``replaces`` maps a refund lot to the lot that paid
    for the session it refunds. A refund of a refund follows the chain to the
    first credit that was not one.

    **A refund whose origin cannot be found counts as bonus**: a session booked
    before the ledger had a debit, or a broken chain. Bonus is the side that
    never claims to be part of the monthly allowance.
    """
    seen: set[UUID] = set()
    current = lot_id
    while current not in seen:
        seen.add(current)
        source = sources.get(current)
        if source is None:
            return "bonus"
        bucket = SOURCE_BUCKET[source]
        if bucket is not None:
            return bucket
        origin = replaces.get(current)
        if origin is None:
            return "bonus"
        current = origin
    return "bonus"


def split_buckets(
    held: Iterable[HeldLot],
    *,
    sources: Mapping[UUID, CreditSource],
    replaces: Mapping[UUID, UUID],
    ceiling: int,
) -> tuple[MonthlyCredits, BonusCredits]:
    """Split the held lots into the card's monthly and bonus parts.

    The two balances add up to the total, because ``held`` is exactly the set of
    lots the total sums.
    """
    monthly = 0
    monthly_expiries: list[dt.datetime] = []
    bonus: dict[dt.datetime | None, int] = {}
    for lot in held:
        if bucket_of(lot.lot_id, sources, replaces) == "monthly":
            monthly += lot.remaining
            if lot.expires_at is not None:
                monthly_expiries.append(lot.expires_at)
        else:
            bonus[lot.expires_at] = bonus.get(lot.expires_at, 0) + lot.remaining

    groups = tuple(
        BonusGroup(count=count, expires_at=expires_at)
        for expires_at, count in sorted(
            bonus.items(),
            key=lambda item: (item[0] is None, item[0].timestamp() if item[0] else 0.0),
        )
    )
    return (
        MonthlyCredits(
            balance=monthly,
            ceiling=ceiling,
            expires_at=min(monthly_expiries) if monthly else None,
        ),
        BonusCredits(balance=sum(bonus.values()), groups=groups),
    )

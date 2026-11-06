"""What a credit is, where one comes from, and when it dies.

Pure, and in ``domain`` because every line is a product rule. Several things
grant credits — the level grants, refunds, admin grants and the migration — and
if each decided its own expiry they would drift. The rule is written once here
and read from there, which is non-negotiable #8 applied before the second copy
exists rather than after.

CREDIT LEVELS (decision 308)
============================
**A mentee's month pays their level, and the 1st resets everyone to it** (owner,
2026-11-05, replacing the #363 top-up plan). The level is what they have done:

    not onboarded, or not a mentee              0
    finished onboarding                         1   (ladder.starter)
    + an invite of theirs qualified             2   (ladder.invited)
    + attended a session too (the unlock, #295) 3   (ladder.monthly)

Every level credit dies at the month end, so nothing carries over and the 1st
needs no arithmetic beyond granting the level. Reaching a higher level mid-month
through one of its events — finishing onboarding, an invite qualifying, the
unlock — tops the month up to it; any other change counts from the next 1st.
**The level is a monthly allowance, not a balance to refill**: what a month
pays is ``level - already granted this period``, so a rise after spending pays
the difference and a re-run pays nothing. Refunds and admin grants are outside
the allowance and may lift a balance above it until the next reset.

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
    "HISTORICAL_SOURCES",
    "LEVEL_SOURCES",
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
    "LevelFacts",
    "MonthlyCredits",
    "allowance_for",
    "bucket_of",
    "card_level",
    "credit_ladder",
    "end_of_month",
    "expiry_for",
    "level_allowance",
    "level_of",
    "level_owed",
    "refund_expiry",
    "split_buckets",
    "state_for",
]


#: Sources whose lots outlive the monthly reset. **Empty since decision 308**:
#: every credit dies at the month end, the starter included. Kept as a set
#: because purchased lots join it when payments land — expiring something
#: somebody bought is a chargeback (D7) — and `refund_expiry` already carries a
#: never-expiring original through.
NON_EXPIRING: frozenset[CreditSource] = frozenset()

#: Sources rows still carry but nothing writes any more (decision 308): the
#: one-off invite bonus, replaced by level 2. Read as a level credit
#: (`SOURCE_BUCKET`), so a lot written before the change still counts toward
#: its month's level and is not paid a second time. (The starter is not here:
#: the migration still writes it, as the cutover month's level-1 credit.)
HISTORICAL_SOURCES: frozenset[CreditSource] = frozenset({CreditSource.REFERRAL_UNLOCK})


@dataclass(frozen=True, slots=True)
class CreditLadder:
    """What each level is worth per month (decision 308).

    **One object rather than three loose numbers**, because the three are only
    meaningful together: a level never pays less than the one below it, which
    `core.config` enforces at startup.
    """

    #: Level 1: finished onboarding.
    starter: int
    #: Level 2: an invite of theirs qualified, before they have attended.
    invited: int
    #: Level 3: invited and attended (the unlock, decision 295). Also the cap on
    #: what a migrated user may carry in and on one admin grant.
    monthly: int


def credit_ladder(settings: Settings) -> CreditLadder:
    """The configured ladder, from settings the caller already holds.

    Configuration because the economics are still moving, not because the rules
    are: what each level *means* — which facts reach it, that it resets on the
    1st — stays here in `domain` (:func:`level_allowance`), and only the sizes
    come from it.

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
        # The variable keeps its name so no deployment has to change; since
        # decision 308 it sizes level 2 rather than a one-off bonus.
        invited=settings.credit_referral_unlock_grant,
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


# --------------------------------------------------------------------------
# Levels (decision 308)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LevelFacts:
    """What a user has done, as the level reads it. Measured in ``infra``
    (``credit_eligibility``), once; judged here, once."""

    #: A mentee who finished onboarding.
    onboarded: bool
    #: An invite of theirs qualified (its invitee finished onboarding).
    invited: bool
    #: A mentee holding the unlock: a qualified invite and an attended session
    #: (decision 295), or grandfathered from the migration.
    unlocked: bool


def level_of(facts: LevelFacts) -> int:
    """Which level the facts reach, 0 to 3 — the number, not what it pays.

    **The unlock reaches level 3 on its own.** ~1,200 migrated mentees hold a
    grandfathered unlock with no invite behind it and no onboarding row the API
    wrote; they received the monthly grant before this decision and still do.

    **An invite counts only once its inviter has onboarded.** Level 2 is level 1
    plus an invite, so an invite alone, from somebody who never finished their
    own profile, is level 0 until they do — and then level 2 at once.
    """
    if facts.unlocked:
        return 3
    if not facts.onboarded:
        return 0
    return 2 if facts.invited else 1


def level_allowance(facts: LevelFacts, ladder: CreditLadder) -> int:
    """What this user's month pays: the ladder's amount for :func:`level_of`.

    **Derived from the level number, never a second copy of the rule**
    (non-negotiable #8). The two differ as soon as the ladder leaves its
    defaults: at a monthly allowance of 5, level 3 pays 5 (found by GitHub
    review on #576).
    """
    return (0, ladder.starter, ladder.invited, ladder.monthly)[level_of(facts)]


def card_level(level: int, ladder: CreditLadder) -> int:
    """The level the card draws: the user's, but **never below level 1's**.

    A caller at level 0 — not onboarded, or a mentor with no mentee goal —
    sees the bar for the first level they can earn. Drawn at 0 the card would
    publish ``allowance: 0`` and ``ceiling: 0``, and a client dividing balance
    by them gets nothing to draw (found by code review); every caller has the
    card, so every caller needs a bar with at least one position.
    """
    return max(level, ladder.starter)


def level_owed(level: int, granted_this_period: int) -> int:
    """How much to grant now so this period has paid ``level`` in all.

    **Never negative, and never a refill.** ``granted_this_period`` counts what
    the level already paid this month, spent or not: a re-run of the 1st pays
    nothing even after the credits were used, and a rise from 1 to 2 pays one.
    """
    return max(0, level - granted_this_period)


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


def state_for(balance: int, level: int) -> CreditState:
    """Which band a balance falls in, against the user's ``level`` allowance.

    **The top band is open-ended** — at or above the level. A refund or an admin
    grant can lift a balance past it, so a table written as ``3..4`` leaves five
    unclassified, and an unclassified balance renders as an empty card rather
    than a full one.

    **The bands follow the level, never literals.** Literals were right for one
    ladder and silently wrong the moment it moved, leaving the two derived
    values on one card disagreeing about one balance.

    Zero and one stay absolute. They are not proportions — *nothing left* and
    *one left* mean the same thing whatever the level is — so a level-1 mentee
    holding their one credit reads ``LOW``: one is all that stands between them
    and a refused booking.

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
    if balance < level:
        return CreditState.MODERATE
    return CreditState.ON_TRACK


def allowance_for(balance: int, level: int) -> int:
    """What the card divides by — the denominator its progress bar draws.

    ``max(level, balance)``: the user's level allowance, a **fixed** ceiling, so
    the bar moves when a credit is spent. A denominator that tracked the balance
    drew a full bar at every balance — the number changed and the picture did
    not.

    It still rises past the level rather than clamping, because **the bar cannot
    draw more segments than it has**: a migrated user may arrive above their
    level, and a refund or an admin grant can lift a balance past it. Clamping
    would read "4 credits left" beside a bar with three positions.

    The server publishes ``balance`` and ``allowance`` and never a percentage —
    the client draws, and a third representation of one fact is the first to
    drift.
    """
    return max(level, balance)


# --------------------------------------------------------------------------
# Monthly and bonus credits (decision 232)
# --------------------------------------------------------------------------

#: The two parts of the card. The bar draws only the monthly part — the level's
#: credits — against the level, so a full month reads "3 of 3" (#344).
Bucket = Literal["monthly", "bonus"]

#: Which part a lot of each source sits in. **One mapping**; a test pins every
#: `CreditSource` member to it.
#:
#: ``REFUND`` maps to ``None`` because a refund has no bucket of its own: it
#: goes back to the bucket of the credit it replaces (owner, 2026-10-03), found
#: by :func:`bucket_of`. ``OPENING_BALANCE`` is monthly because it stands in for
#: the cutover month's grant, which is why it is capped at the grant.
#:
#: **Since decision 308 every level grant is monthly**: the 1st's grant, a
#: mid-month top-up, and the starter and invite lots written before it, which
#: were the first two levels' credits under their old names. Only an admin
#: grant is bonus — outside the allowance, and the one credit the level does not
#: count.
SOURCE_BUCKET: Mapping[CreditSource, Bucket | None] = MappingProxyType(
    {
        CreditSource.MONTHLY_FREE: "monthly",
        CreditSource.LEVEL_TOP_UP: "monthly",
        CreditSource.OPENING_BALANCE: "monthly",
        CreditSource.PROFILE_COMPLETED: "monthly",
        CreditSource.REFERRAL_UNLOCK: "monthly",
        CreditSource.ADMIN_GRANT: "bonus",
        CreditSource.REFUND: None,
    }
)

#: The sources whose lots count toward a period's level allowance: exactly the
#: monthly bucket. **Derived, not listed**, so a source cannot be on the card's
#: monthly bar and missing from what the level counts (non-negotiable #8).
#: Refunds are excluded because their bucket is ``None``: a refund returns what
#: was paid, and counting it would let a refund shrink the next top-up.
LEVEL_SOURCES: frozenset[CreditSource] = frozenset(
    source for source, bucket in SOURCE_BUCKET.items() if bucket == "monthly"
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
    #: The user's level allowance (decision 308): what their month pays. A
    #: refund can briefly put ``balance`` above it.
    ceiling: int
    #: The soonest instant a held monthly credit stops being spendable. Null when
    #: there are none, or none of them expire.
    expires_at: dt.datetime | None
    #: Whether the account holds the unlock — level 3, the full monthly grant.
    #: The store sets it from the same fact the level reads.
    unlocked: bool = False
    #: The user's level number, 0 to 3 (:func:`level_of`) — **not floored**,
    #: unlike ``ceiling`` (:func:`card_level`), so 0 ("not onboarded") is told
    #: apart from 1. The store sets it.
    level: int = 0


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
            expires_at=min(monthly_expiries) if monthly and monthly_expiries else None,
        ),
        BonusCredits(balance=sum(bonus.values()), groups=groups),
    )

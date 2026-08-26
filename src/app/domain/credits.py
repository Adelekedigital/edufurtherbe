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
**No ``purchase``, no ``promotional``, no ``admin_grant``.** The canonical DDL
declares seven sources; settled decision #21 names this exact enum as its
cautionary example, because `credit_source` "contains `purchase` while payments
are out of scope by decision #8". A vocabulary is not a wish list: PostgreSQL
cannot remove a value from a native enum at all, and while #100 moved these to
``text`` + ``CHECK`` — which *is* freely alterable — a shipped member still
invites a writer, and the reason to defer is that nothing writes one.

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

from app.domain.enums import CreditReason, CreditSource, CreditState

__all__ = [
    "MONTHLY_ALLOWANCE",
    "NON_EXPIRING",
    "STARTER_GRANT",
    "STEADY_STATE",
    "UNLOCK_GRANT",
    "CreditReason",
    "CreditSource",
    "CreditState",
    "allowance_for",
    "end_of_month",
    "expiry_for",
    "refund_expiry",
    "state_for",
]


#: Sources whose lots outlive the monthly reset. One today; every purchased lot
#: joins it when payments land, which is why this is a set rather than an
#: ``is PROFILE_COMPLETED`` comparison at the two call sites.
NON_EXPIRING: frozenset[CreditSource] = frozenset({CreditSource.PROFILE_COMPLETED})

#: Finishing onboarding.
STARTER_GRANT = 1
#: Added by the first qualifying invite, on top of the starter.
UNLOCK_GRANT = 2
#: Granted on the 1st to every unlocked mentee, and the card's denominator —
#: except when a late refund pushes a balance above it, which is why the read
#: publishes ``max(allowance, balance)`` rather than this number alone.
MONTHLY_ALLOWANCE = 3

#: What the card's progress bar divides by — **the ceiling, not the grant**.
#:
#: The non-expiring starter plus the monthly three, which is where a settled,
#: unlocked mentee sits on the 1st. The bar is a position marker whose filled
#: segment is the balance, so the denominator has to be a *fixed* ceiling: with
#: `max(MONTHLY_ALLOWANCE, balance)` the denominator tracked the numerator and
#: `balance == allowance` for every balance at or above three — a four-segment
#: bar full at four, then a three-segment bar still full at three. **The bar did
#: not move when a credit was spent.** Found by code review.
#:
#: Migrated users arrive at five (29 of 43 in the dev export), which is why
#: `allowance_for` still raises this rather than clamping to it.
STEADY_STATE = STARTER_GRANT + MONTHLY_ALLOWANCE


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


def state_for(balance: int) -> CreditState:
    """Which band a balance falls in.

    **The top band is open-ended** — four *or more*. A late refund can land a
    credit after the monthly grant, so a table written as ``4..5`` leaves six
    unclassified, and an unclassified balance renders as an empty card rather
    than a full one.

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
    if balance <= 3:
        return CreditState.MODERATE
    return CreditState.ON_TRACK


def allowance_for(balance: int) -> int:
    """What the card divides by — the denominator its progress bar draws.

    ``max(STEADY_STATE, balance)`` — the **ceiling**, not the monthly grant.

    A fixed denominator is what makes the bar move. Dividing by
    ``MONTHLY_ALLOWANCE`` made ``balance == allowance`` for every balance at or
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
    return max(STEADY_STATE, balance)

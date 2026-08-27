"""Turning a legacy ``bookingCredit`` string into the balance a user arrives with.

**One field decides everything here, and it is a string.** Bubble stores
``bookingCredit`` as text, so ``''`` and ``'0'`` are different values — five dev
users hold the first and two hold the second — and the difference is the whole
transform:

    ''      never entered the credit system   -> the starter, if they finished
    '0'     entered it and spent down          -> nothing
    '1'..   a balance they still hold          -> an `opening_balance` lot

Collapsing the two with ``int(raw or 0)`` is the version that looks right and
quietly denies four dev users a credit they should have. It is settled as
measured finding 5 in the credit handoff.

WHAT DOES NOT MIGRATE, AND WHY IT IS NOT AN OMISSION
====================================================
**``bookingCreditRenewDate`` is dropped.** ADR 0027 §1 and measured findings 1-3:
legacy renewal was a *per-user scheduled workflow*, not a monthly grant.
``bookingCredit-WFCode`` is populated on 27 of 43 dev users and is Bubble's
handle for that workflow; the date is only its last footprint, and 16 users hold
a stale one with no workflow behind it. Carrying the date would hand each user a
personal reset day that the dashboard card — which says the 1st — then
contradicts on screen. Twenty-five of the thirty-six users who get a lot have a
renew date already in the past, and fourteen share one timestamp from a single
bulk run.

So every migrated lot expires **end of the cutover month**, uniformly, which is
what the earning ladder specifies and what the card can truthfully render.

**``bookingCredit-WFCode`` itself is dropped**, being the artifact rather than
the entitlement.

NOBODY ARRIVES ABOVE THE MONTHLY ALLOWANCE
==========================================
**A migrated balance is capped at** the configured monthly grant
(:func:`~app.domain.credits.credit_ladder`).
The legacy grant was five a month; the new one is three, and somebody carrying
five into a platform that grants three would sit above the ceiling the card can
draw until they spent the difference.

So five becomes three, and so does anything above five. **Four also becomes
three**, which the instruction did not name explicitly — the rule is stated as a
cap rather than as "five or more", because "four keeps four while five drops to
three" is a rule with no reason behind it. One dev user is affected. Say the word
and it becomes ``if quantity >= 5`` instead.

Written against the constant rather than a literal ``3``, so a change to the
allowance moves the cap with it — including if the allowance later becomes
configuration.

WHAT THE CEILING IS NOW FOR
===========================
:data:`PLAUSIBLE_CEILING` was a safety guard: before the cap, a corrupt ``'500'``
would have loaded as five hundred credits. The cap removes that danger entirely,
so **it no longer quarantines** — a user whose balance is unreadable-high still
gets their three, like everyone else, and the anomaly is *reported* so somebody
can look at the source row while Bubble is still writable.

Refusing them a lot over a data problem they did not create would have been the
wrong trade once the value could no longer hurt anything.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.domain.bubble import blank_to_none, legacy_anchor
from app.domain.credits import credit_ladder, end_of_month

__all__ = [
    "ACTIVE_WITHIN",
    "CREDIT_FIELD",
    "PLAUSIBLE_CEILING",
    "RENEW_DATE_FIELD",
    "CreditPlan",
    "OpeningLotRow",
    "QuarantinedBalance",
    "StarterRow",
    "plan_opening_balances",
]

#: The legacy field, under the name the export writes.
CREDIT_FIELD = "bookingCredit"

#: Named so the docstring above can point at it, and so a reader grepping for it
#: finds the reason it is unused rather than nothing at all.
RENEW_DATE_FIELD = "bookingCreditRenewDate"

#: Above this, a value is **reported but still loaded** — capped like every other
#: balance. Not a product rule: a tripwire chosen from the dev maximum of 5, kept
#: only so a corrupt export is visible. See the module docstring.
PLAUSIBLE_CEILING = 10

#: How recently somebody must have used the platform to be grandfathered into
#: the recurring grant. **Twelve months, and this is the number to change.**
#:
#: The owner asked for "six to twelve"; the two ends are not close together. On
#: the dev export — stale test data, so indicative of shape rather than of
#: production — six months qualifies 1 user of 43 and twelve qualifies 17. The
#: generous end is the default because the two errors are not symmetric:
#: grandfathering somebody dormant costs three credits a month that nobody
#: spends, while missing somebody active silently switches off a benefit they
#: have today, which is the exact failure `ReferralUnlock` was made nullable to
#: avoid.
#:
#: The dry run prints how many qualify, so the production number is visible
#: before the freeze rather than after it.
ACTIVE_WITHIN = dt.timedelta(days=365)


@dataclass(frozen=True, slots=True)
class OpeningLotRow:
    """A balance a user carries out of Bubble."""

    user_bubble_id: str
    quantity: int
    expires_at: dt.datetime


@dataclass(frozen=True, slots=True)
class StarterRow:
    """A user who never entered the credit system and has finished onboarding.

    **Not an opening balance.** They arrive with the credit a new user earns, on
    the source that says so — `profile_completed`, quantity one, never expiring.
    Writing them an `opening_balance` lot of 1 would claim Bubble held a balance
    it did not, and the reconciliation that compares the lot sum against the
    legacy sum would then be wrong by exactly the number of them.
    """

    user_bubble_id: str


@dataclass(frozen=True, slots=True)
class QuarantinedBalance:
    """A value nobody predicted, and the reason in words.

    **Reported, never coerced.** A `bookingCredit` this transform cannot read is
    somebody's balance; guessing at it either invents credits or destroys them,
    and both are invisible once loaded.
    """

    user_bubble_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class CreditPlan:
    """Everything one extract turns into, before any of it is written."""

    lots: tuple[OpeningLotRow, ...] = ()
    starters: tuple[StarterRow, ...] = ()

    #: Balances above :data:`PLAUSIBLE_CEILING`. **Loaded anyway**, capped like
    #: the rest — this is a data-quality signal for the operator, not a refusal.
    implausible: tuple[str, ...] = ()

    #: Users the transform deliberately gives nothing, split by *why*. Two
    #: separate counts rather than one "skipped", because they are different
    #: facts about the source and an operator reading the report needs to see
    #: that `''` and `'0'` were told apart — which is the one thing this
    #: transform exists to get right.
    spent_down: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()

    #: Migrated users who keep receiving the monthly grant. **Every migrated
    #: user the platform has seen recently**, whatever their legacy balance was
    #: — including those who never entered the credit system at all.
    #:
    #: The balance is not a condition, and an earlier draft made it one. The
    #: legacy app granted monthly credits *unconditionally* (measured finding 4),
    #: so "was in the credit system" describes when somebody joined rather than
    #: what they were entitled to — and gating on it would cut off every recent
    #: signup who had not yet been granted anything.
    #:
    #: `ReferralUnlock` was made nullable in #209 for exactly these rows: *"~1,200
    #: migrated users are grandfathered as unlocked and none of them ever invited
    #: anybody"*. The schema was ready; this is the writer it was waiting for.
    #:
    #: **They step from five to three.** The legacy grant was five a month,
    #: unconditionally; the new one is the configured monthly grant. Nothing changes
    #: the amount — it is simply what the grant gives.
    #:
    #: **Mentee-ness is not checked here.** `grant_monthly_credits` already
    #: requires a `MenteeGoal`, so a migrated mentor holding an unlock receives
    #: nothing. Adding the predicate here as well would be one rule in two
    #: places, and this one would be reading a table the credit loader has no
    #: other reason to know about.
    grandfathered: tuple[str, ...] = ()

    #: Every anchor the export offered. **Reconciliation needs a source list to
    #: be missing from**: comparing the plan against itself agrees with itself,
    #: which `reconcile.py` records shipping once.
    source_anchors: tuple[str, ...] = ()

    quarantined: tuple[QuarantinedBalance, ...] = ()

    @property
    def source_rows(self) -> int:
        return len(self.source_anchors)

    @property
    def legacy_credit_total(self) -> int:
        """What Bubble said these users held, added up.

        The left-hand side of the reconciliation the Definition of Done asks
        for: this must equal the sum of `quantity_granted` over the loaded
        `opening_balance` lots. **Granted, not remaining** — the two are equal at
        cutover, but only the former stays true once somebody spends a credit,
        and a check that starts failing on a correctly migrated database is worse
        than no check.

        Starters are deliberately not in it: they are not a migrated balance, and
        folding them in would make the two sides agree only if the legacy total
        were wrong by exactly the number of starters.
        """
        return sum(row.quantity for row in self.lots)

    def report(self) -> str:
        """The operator-facing account of one run.

        Lives in `domain` beside the decisions it summarises (settled decision
        #45). The quarantine block is the deliverable rather than a footnote:
        each line is a balance that did not migrate and the reason, which is the
        only form in which somebody can go and look at the source row.
        """
        lines = [
            f"source rows: {self.source_rows}",
            f"opening balances to load: {len(self.lots)} carrying "
            f"{self.legacy_credit_total} credit(s)",
            f"starters to grant: {len(self.starters)}",
            f"spent down, nothing owed: {len(self.spent_down)}",
            f"grandfathered into the monthly grant: {len(self.grandfathered)}",
            f"implausibly large, capped and loaded: {len(self.implausible)}",
            f"never entered credits and unfinished: {len(self.unfinished)}",
            f"quarantined: {len(self.quarantined)}",
        ]
        lines += [f"  QUARANTINED {row.user_bubble_id}: {row.reason}" for row in self.quarantined]
        lines += [
            f"  IMPLAUSIBLE {anchor}: above {PLAUSIBLE_CEILING}, capped to "
            f"{credit_ladder().monthly} and loaded"
            for anchor in self.implausible
        ]
        return "\n".join(lines)


def _text(value: Any) -> str | None:
    """A trimmed value, or ``None`` for anything the export renders as empty.

    **This is what makes ``''`` and an absent key the same fact**, which they
    are: both mean Bubble never wrote a number here. It is `'0'` that must stay
    distinct, and it survives because it is not blank.
    """
    cleaned = blank_to_none(value)
    return str(cleaned).strip() if cleaned is not None else None


def plan_opening_balances(
    records: list[dict[str, Any]],
    *,
    cutover: dt.datetime,
    finished: frozenset[str],
    seen: frozenset[str] = frozenset(),
) -> CreditPlan:
    """Every balance the export offers, as a lot, a starter, or a reason.

    Pure: no I/O, no database, no clock. ``cutover`` is a parameter because the
    expiry is *end of the month the migration runs in*, which the caller knows
    and this does not; ``finished`` is the set of anchors whose onboarding is
    complete, because whether somebody earned the starter is a fact about the
    onboarding table rather than about this field.

    **``finished`` is passed rather than read from the record.**
    ``user_onboarding.completed_at`` is what the starter's own producer keys on,
    and `identity.py` already derives it from ``'registration completed '``.
    Re-reading the raw field here would be a second representation of "has this
    person finished", and the two would disagree the first time that derivation
    changed.

    ``seen`` is the same shape and there for the same reason: anchors last
    active — **or newly signed up** — inside :data:`ACTIVE_WITHIN` of the
    cutover. Read from the columns rather than from the export, because they are
    what every other "is this person around" question in the codebase uses.

    **Defaulted to empty, which grandfathers nobody.** A caller that forgets to
    pass it writes no unlocks — the cliff, not a silent over-grant. Handing out
    a recurring benefit is the error that is expensive to undo.
    """
    monthly = credit_ladder().monthly
    expires_at = end_of_month(cutover)

    lots: list[OpeningLotRow] = []
    starters: list[StarterRow] = []
    grandfathered: list[str] = []
    spent_down: list[str] = []
    unfinished: list[str] = []
    implausible: list[str] = []
    quarantined: list[QuarantinedBalance] = []
    source_anchors: list[str] = []
    decided: set[str] = set()

    for record in records:
        anchor = legacy_anchor(record)
        source_anchors.append(anchor or "<no id>")

        if not anchor:
            quarantined.append(QuarantinedBalance("<no id>", "the row carries no unique id"))
            continue
        if anchor in decided:
            # The lot is guarded by a partial unique index on the user, so a
            # repeated anchor would collapse into one lot silently — and the
            # reconciliation keys on the same anchor, so it would agree.
            quarantined.append(QuarantinedBalance(anchor, "the export repeats this unique id"))
            continue
        decided.add(anchor)

        # **Decided here, before the balance is read, and that is the point.**
        # Every migrated user the platform has seen recently keeps the recurring
        # grant. The legacy app granted monthly credits *unconditionally*
        # (measured finding 4), so what somebody's `bookingCredit` happens to say
        # describes when they joined rather than what they are entitled to.
        #
        # A quarantined balance therefore does not cost somebody their
        # entitlement — it costs them the lot, which is the thing an operator
        # looks at.
        if anchor in seen:
            grandfathered.append(anchor)

        raw = _text(record.get(CREDIT_FIELD))

        if raw is None:
            # Never entered the credit system. The starter is theirs if they
            # finished onboarding; if not, they earn it the ordinary way when
            # they do, and granting it now would pay for work not yet done.
            if anchor in finished:
                starters.append(StarterRow(anchor))
            else:
                unfinished.append(anchor)
            continue

        try:
            quantity = int(raw)
        except ValueError:
            quarantined.append(
                QuarantinedBalance(
                    anchor, f"{CREDIT_FIELD} is {raw!r}, which is not a whole number"
                )
            )
            continue

        if quantity < 0:
            quarantined.append(
                QuarantinedBalance(anchor, f"{CREDIT_FIELD} is {quantity}, which is negative")
            )
            continue
        if quantity == 0:
            # Entered the credit system and spent it. The absence of a lot is
            # the whole record — a lot of zero would fail
            # `ck_credit_lots_quantity_granted_positive` and would be a row
            # saying nothing happened.
            #
            # **Still grandfathered**, and that is the point of doing it here
            # rather than off `lots`: `'0'` means they were in the credit system
            # and reached zero. `''` means they never entered it. Only the second
            # is a reason not to keep granting.
            spent_down.append(anchor)
            continue
        if quantity > PLAUSIBLE_CEILING:
            # Reported, not refused. The cap below makes the value harmless, and
            # denying somebody their credits over a data problem they did not
            # create would be the wrong trade.
            implausible.append(anchor)

        # **The cap.** Nobody arrives holding more than a month's grant.
        lots.append(OpeningLotRow(anchor, min(quantity, monthly), expires_at))

    return CreditPlan(
        lots=tuple(lots),
        starters=tuple(starters),
        grandfathered=tuple(grandfathered),
        implausible=tuple(implausible),
        spent_down=tuple(spent_down),
        unfinished=tuple(unfinished),
        source_anchors=tuple(source_anchors),
        quarantined=tuple(quarantined),
    )

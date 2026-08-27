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

THE CEILING IS A TRIPWIRE, NOT A PRODUCT RULE
=============================================
:data:`PLAUSIBLE_CEILING` is not something the product says; it is the point past
which a value is more likely to be corruption than an entitlement. The dev
maximum is 5 and the steady state is 4, so ten leaves generous headroom while
still refusing ``'500'``.

A quarantine here is not a refusal to migrate somebody — it is a row an operator
looks at *while Bubble is still writable*, which is the whole reason the dry run
exists. If production legitimately holds a larger balance, the answer is to raise
this constant deliberately, not to have loaded it silently.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.domain.bubble import blank_to_none, legacy_anchor
from app.domain.credits import end_of_month

__all__ = [
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

#: Above this, a value is reported rather than loaded. See the module docstring —
#: this is a tripwire chosen from the dev maximum of 5, not a product rule.
PLAUSIBLE_CEILING = 10


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

    #: Users the transform deliberately gives nothing, split by *why*. Two
    #: separate counts rather than one "skipped", because they are different
    #: facts about the source and an operator reading the report needs to see
    #: that `''` and `'0'` were told apart — which is the one thing this
    #: transform exists to get right.
    spent_down: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()

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
        for: this must equal the sum of `quantity_remaining` over the loaded
        `opening_balance` lots. Starters are deliberately not in it — they are
        not a migrated balance.
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
            f"never entered credits and unfinished: {len(self.unfinished)}",
            f"quarantined: {len(self.quarantined)}",
        ]
        lines += [f"  QUARANTINED {row.user_bubble_id}: {row.reason}" for row in self.quarantined]
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
    """
    expires_at = end_of_month(cutover)

    lots: list[OpeningLotRow] = []
    starters: list[StarterRow] = []
    spent_down: list[str] = []
    unfinished: list[str] = []
    quarantined: list[QuarantinedBalance] = []
    source_anchors: list[str] = []
    seen: set[str] = set()

    for record in records:
        anchor = legacy_anchor(record)
        source_anchors.append(anchor or "<no id>")

        if not anchor:
            quarantined.append(QuarantinedBalance("<no id>", "the row carries no unique id"))
            continue
        if anchor in seen:
            # The lot is guarded by a partial unique index on the user, so a
            # repeated anchor would collapse into one lot silently — and the
            # reconciliation keys on the same anchor, so it would agree.
            quarantined.append(QuarantinedBalance(anchor, "the export repeats this unique id"))
            continue
        seen.add(anchor)

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
            spent_down.append(anchor)
            continue
        if quantity > PLAUSIBLE_CEILING:
            quarantined.append(
                QuarantinedBalance(
                    anchor,
                    f"{CREDIT_FIELD} is {quantity}, above the plausible ceiling of "
                    f"{PLAUSIBLE_CEILING} — look at the source row before raising it",
                )
            )
            continue

        lots.append(OpeningLotRow(anchor, quantity, expires_at))

    return CreditPlan(
        lots=tuple(lots),
        starters=tuple(starters),
        spent_down=tuple(spent_down),
        unfinished=tuple(unfinished),
        source_anchors=tuple(source_anchors),
        quarantined=tuple(quarantined),
    )

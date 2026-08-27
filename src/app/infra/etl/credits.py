"""Writing the balances users carry out of Bubble, and the ledger that explains them.

**Two writes per lot, and the second is not optional.** A lot without a matching
`grant` row is a balance that rose with nothing saying why — the state D8 chose a
ledger over a counter to prevent — and the reconciliation the Definition of Done
asks for (*the lot sum equals the legacy sum*) would be comparing against a
table that had already broken its own rule.

IDEMPOTENCE COMES FROM TWO PARTIAL INDEXES, NOT FROM `legacy_bubble_id`
=======================================================================
Every other ETL-loaded table carries `legacy_bubble_id` and upserts on it.
`credit_lots` does not, deliberately — the model says why: *"that column makes a
row re-runnable, while the invariant here is one lot per user"*. So:

    uq_credit_lots_one_opening_balance_per_user   one migrated balance, ever
    uq_credit_lots_one_starter_per_user           one starter, ever

Both are partial unique indexes on `user_id`, and `ON CONFLICT DO NOTHING`
against each is what makes the second run a no-op. The runbook rehearses twice
and the real cutover is a third, so that is the normal path rather than an edge
case — and without it every migrated user's balance would double on the
rehearsal.

**The ledger rows are derived from the lots that exist**, not from the plan.
`grant_monthly_credits` takes the same care and states the reason: the two differ
by exactly the rows the conflict clause skipped, and writing a movement for a lot
that was not inserted is a balance claiming to have moved when it did not.

NO TIMESTAMP IS TAKEN FROM THE SOURCE
=====================================
`loader.py` exists almost entirely to hold `trg_set_updated_at` off so Bubble's
timestamps survive a re-run. Nothing here needs that: **a lot is created at
cutover**, not in 2025. There is no source timestamp for a lot — `bookingCredit`
is a number on a user, with no creation date of its own — so `now()` is the
truthful value and the trigger is left alone.

WHAT THIS DOES NOT WRITE
========================
No lot for `''` users who have not finished onboarding, and none for `'0'`. Both
are decisions the transform records and this module obeys; putting the reasoning
in two places is how the two come to disagree.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import text

from app.domain.credits import CreditLadder
from app.domain.enums import CreditReason, CreditSource
from app.domain.transform.credits import CreditPlan

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["CreditLoader", "finished_onboarding", "recently_seen"]


#: **The starter's own condition, read from the table its producer keys on.**
#:
#: `grant_starter` fires when somebody completes onboarding, and
#: `user_onboarding.completed_at` is the record of that. A migrated user who
#: finished in Bubble has the column set by `satellites.py`, so this is the same
#: question asked of the same fact — not a second rule derived from the raw
#: ``'registration completed '`` field, which would be free to disagree the
#: first time `identity.py` changed how it reads one.
FINISHED_ANCHORS = """
SELECT u.legacy_bubble_id AS anchor
FROM users u
JOIN user_onboarding o ON o.user_id = u.id
WHERE u.legacy_bubble_id IS NOT NULL AND o.completed_at IS NOT NULL
"""


#: **Read from the columns, not from the export's ``Last Active``.**
#: ``users.last_active_at`` is what every other "is this person around" question
#: in this codebase uses, and `load_identity` has already written both of these
#: by the time this runs. Deriving them a second way here is how the two come to
#: disagree.
#:
#: **Or signed up**, which is not the same question and is why this is a
#: disjunction. Somebody who registered three weeks ago and has not returned has
#: a recent ``created_at`` and possibly no ``last_active_at`` at all — they are
#: exactly the new joiner the platform is trying to keep, and an activity-only
#: filter would cut them off at the first month end.
RECENTLY_SEEN_ANCHORS = """
SELECT u.legacy_bubble_id AS anchor
FROM users u
WHERE u.legacy_bubble_id IS NOT NULL
  AND (u.last_active_at >= :since OR u.created_at >= :since)
"""


async def recently_seen(connection: AsyncConnection, *, since: dt.datetime) -> frozenset[str]:
    """Anchors last active — or newly signed up — on or after ``since``.

    **A null ``last_active_at`` is not a disqualification**, because the second
    disjunct still answers for them: ``created_at`` is ``NOT NULL`` on every
    row. A user who never triggered the activity column but registered inside
    the window qualifies on their signup, which is the intent.
    """
    result = await connection.execute(text(RECENTLY_SEEN_ANCHORS), {"since": since})
    return frozenset(row.anchor for row in result)


async def finished_onboarding(connection: AsyncConnection) -> frozenset[str]:
    """Anchors whose onboarding is complete, for the transform to decide starters.

    Read rather than derived, for the reason `user_id_map` gives about ids: the
    fact lives in the database after `load_identity` ran, and re-deriving it here
    would make this loader's answer depend on a transform it does not call.
    """
    result = await connection.execute(text(FINISHED_ANCHORS))
    return frozenset(row.anchor for row in result)


#: **Literals, not interpolation.** The migrations already take this shape and
#: say why: interpolating an enum's ``.value`` into a statement is the
#: f-string-into-SQL pattern the security checklist names, and a suppression
#: would be one more rule nobody reads. What keeps these honest is
#: :func:`tests.unit.test_credit_etl_sql` — it asserts every literal below is a
#: member of the enum it stands for, so renaming a member turns this red rather
#: than shipping SQL that names a value the database will reject.
#:
#: **Resolved by join, not by a prior lookup.** The anchor becomes a ``user_id``
#: inside the statement, so an anchor with no user inserts nothing rather than
#: raising — which is what lets `reconcile_credits` report it as missing instead
#: of aborting the whole load on the first orphan.
INSERT_OPENING_LOT = """
INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining, expires_at)
SELECT u.id, 'opening_balance', :quantity, :quantity, :expires_at
FROM users u
WHERE u.legacy_bubble_id = :anchor
ON CONFLICT (user_id) WHERE source = 'opening_balance' DO NOTHING
"""

#: The quantity is bound rather than written, so the ladder stays the one
#: place the starter's size is decided.
INSERT_STARTER_LOT = """
INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining, expires_at)
SELECT u.id, 'profile_completed', :quantity, :quantity, NULL
FROM users u
WHERE u.legacy_bubble_id = :anchor
ON CONFLICT (user_id) WHERE source = 'profile_completed' DO NOTHING
"""

#: **One statement for every lot this loader is responsible for**, keyed on the
#: absence of a grant row rather than on what was just inserted.
#:
#: That is what makes it idempotent *and* self-repairing across a rehearsal
#: interrupted between the two writes — a run that inserted lots and died before
#: the ledger leaves rows this fixes on the next attempt, where a
#: ``RETURNING``-driven version would leave them explained by nothing forever.
#:
#: ``reason = 'grant'`` narrows it deliberately: once the expiry sweep has run a
#: lot carries a ``lot_expired`` row, and ``NOT EXISTS (any transaction)`` would
#: then read as "already accounted for" on a lot whose grant is genuinely
#: missing.
INSERT_MISSING_GRANTS = """
INSERT INTO credit_transactions (user_id, credit_lot_id, delta, reason, session_id)
SELECT l.user_id, l.id, l.quantity_granted, 'grant', NULL
FROM credit_lots l
WHERE l.source IN ('opening_balance', 'profile_completed')
  AND NOT EXISTS (
      SELECT 1 FROM credit_transactions t
      WHERE t.credit_lot_id = l.id AND t.reason = 'grant'
  )
"""

#: **The grandfather, and the row `ReferralUnlock` was made nullable for.**
#:
#: #209's model docstring states the intent and the alternative it rejected:
#: *"~1,200 migrated users are grandfathered as unlocked and none of them ever
#: invited anybody … letting every migrated mentee's balance fall to zero at the
#: first month end … silently switches off a benefit people currently have."*
#: The schema was ready; this is the writer it was waiting for.
#:
#: ``unlocked_by_referral_id`` is ``NULL`` — the reason is *absent* rather than
#: invented. A synthetic referral row per user would put an invite that never
#: happened into a table whose whole purpose is evidence.
#:
#: ``ON CONFLICT DO NOTHING`` against ``uq_referral_unlocks_user_id``, so a
#: rehearsal followed by a cutover writes one row, and so a migrated user who
#: has since earned an unlock the ordinary way keeps the one they earned.
INSERT_GRANDFATHER_UNLOCK = """
INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id)
SELECT u.id, NULL
FROM users u
WHERE u.legacy_bubble_id = :anchor
ON CONFLICT (user_id) DO NOTHING
"""

#: The literals above, as the members they must name. The pinning test walks
#: this rather than re-reading the SQL, so adding a statement means adding its
#: vocabulary here — which is the moment somebody notices.
SQL_VOCABULARY = {
    "opening_balance": CreditSource.OPENING_BALANCE,
    "profile_completed": CreditSource.PROFILE_COMPLETED,
    "grant": CreditReason.GRANT,
}


class CreditLoader:
    """Writes opening balances and the starters that stand in for them.

    **Returns nothing.** What landed is read back by `reconcile_credits`; a count
    handed over by the writer is the writer grading its own homework, which is
    the note `ReviewLoader` already carries.
    """

    def __init__(self, connection: AsyncConnection, *, ladder: CreditLadder) -> None:
        self._connection = connection
        self._ladder = ladder

    async def load(self, plan: CreditPlan) -> None:
        """Write every lot the plan calls for, then explain each one.

        Ordered: lots first, ledger second, in one transaction the caller owns.
        A grant row cannot name a lot that does not exist — the composite key
        `fk_credit_transactions_lot_belongs_to_user` refuses it — so the reverse
        order is not merely wrong, it does not run.
        """
        if plan.lots:
            await self._connection.execute(
                text(INSERT_OPENING_LOT),
                [
                    {
                        "anchor": row.user_bubble_id,
                        "quantity": row.quantity,
                        "expires_at": row.expires_at,
                    }
                    for row in plan.lots
                ],
            )

        if plan.starters:
            await self._connection.execute(
                text(INSERT_STARTER_LOT),
                [
                    {"anchor": row.user_bubble_id, "quantity": self._ladder.starter}
                    for row in plan.starters
                ],
            )

        if plan.grandfathered:
            await self._connection.execute(
                text(INSERT_GRANDFATHER_UNLOCK),
                [{"anchor": anchor} for anchor in plan.grandfathered],
            )

        await self._connection.execute(text(INSERT_MISSING_GRANTS))

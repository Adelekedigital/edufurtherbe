# Handoff: monthly credits top up to 3, and bonus credits cap at 3

For a dedicated credits session. Decided by the owner on 2026-10-04 and **not
built yet**; tracked as backend issue #363.
Tier 1: it moves credits.

## The model the owner chose

| Credit | Expires? | Limit |
|---|---|---|
| **Bonus**: starter (`profile_completed`), invite reward (`referral_unlock`), support grants (`admin_grant`), any credit that isn't the monthly grant | **Never** | **Hard cap of 3 bonus credits held.** A bonus grant that would take the holder over 3 is trimmed to fit, or skipped when they already hold 3 |
| **Monthly** (`monthly_free`), for unlocked users only, on the 1st | End of month, as today | **Grant = 3 − bonus credits held at that moment**, never below 0 |

**The effect:** on the 1st of each month an unlocked user holds exactly 3.
- 0 bonus → grant 3.
- 1 bonus → grant 2.
- 3 bonus → grant 0.

The display stays "3" without explaining a split.

**Settled with the owner:**

1. **The cap counts bonus credits only, not the total.** So a user can hold more than 3 mid-month. For example, 3 monthly on the 1st plus an invite reward of 2 on the 10th makes 5 until month end; next month's grant is then 1, and they're back to 3. Accepted, because they earned it and it settles on the next grant.
2. **Refunds are exempt from the cap.** A refund returns what was paid, even if that takes bonus above 3 briefly; the next monthly grant adjusts.
3. **The invite reward stays worth having.** It's still credited, up to the cap, and the referral's purpose is bringing in an interested person, not farming credits.
4. **Spending order is unchanged:** soonest-expiring first and never-expiring last, so monthly credits are spent before bonus.
5. **Rejected alternative:** deducting only the starter, so invite rewards and support grants stay extras on top of 3. The owner chose consistency and the hard cap.

## What changes in the backend

1. **The monthly grant** (`infra/db/credit_grants.py` `grant_monthly_credits`, eligibility in `credit_eligibility.receives_monthly_grant`):
   - Grant `max(0, ladder.monthly − bonus_held)` per user, where `bonus_held` is the user's spendable credits in the bonus bucket at run time (`domain/credits.py` `SOURCE_BUCKET`; refunds count in the bucket of the credit they replaced, per decision 232).
   - A user owed 0 gets no lot and no "credits renewed" email.
   - The email's count is the amount actually granted.
   - Keep the job idempotent: the existing once-per-month conflict guard must still hold when the amount varies.
2. **The bonus cap**, applied wherever a bonus lot is created:
   - the starter grant, the invite reward (`referral_writer.py`) and admin grants (`admin_credits.py`);
   - grant `min(amount, 3 − bonus_held)`, checked **inside the granting transaction under the per-user credit lock** (`credit_writer.py` already takes `CRED`), so two grants can't overshoot together;
   - record the amount actually granted in the ledger;
   - a grant trimmed to 0 writes nothing, and the admin endpoint should say so (e.g. return `granted: 0, capped: true`).

   The cap value belongs in the ladder or config, one named setting, with 3 as the default.
3. **Bonus never expires:** extend `NON_EXPIRING` in `domain/credits.py` to every bonus source (`referral_unlock`, `admin_grant`), so `expiry_for` returns `None` for them. Check `refund_expiry` still does the right thing: a refund of a never-expiring credit never expires, which already holds.
4. **Existing lots:** on dev, clear `expires_at` on held `referral_unlock` and `admin_grant` lots, as a data migration in a proper Alembic migration (expand-safe, idempotent). Decide whether held bonus already above 3 is grandfathered or trimmed; the owner should confirm, and grandfathering is safer.
5. **Docs:**
   - a settled-decision row (the owner's decision, 2026-10-04);
   - amend decision 232's display notes and ADR 0027 (the credit ledger) where they describe expiry and grants;
   - update the `/me` route description.
6. **Unchanged:** spending order, the `/me` response shape (`balance`, `monthly`, `bonus`, `unlocked`), the refund rules (decision 229) and the booking cost.

## Tests the build needs (each failing first)

- **Monthly grant:**
  - 0, 1, 2 and 3 bonus held → grants 3, 2, 1 and 0;
  - a user owed 0 gets no lot and no email;
  - a re-run in the same month pays nothing.
- **The bonus cap:**
  - starter, invite and admin grants each trimmed at the cap;
  - 3 held → skipped with nothing written;
  - two concurrent bonus grants can't exceed 3.
- **Refunds:** a refund of a bonus credit can take bonus above 3; the next monthly grant then pays less, but never below 0.
- **Expiry:** `referral_unlock` and `admin_grant` lots get no expiry; the migration clears expiry on existing ones.
- **Mutations** on the subtraction, the cap comparison and the lock.

## For the frontend

The `/me` credits shape doesn't change. **Display rule** (owner, 2026-10-04): **show monthly credits first; show bonus credits only once the monthly ones are used up.**

That means:
- **A normal month reads "3"**, or "N of 3" as monthly credits are spent.
- **Bonus credits appear** when `monthly.balance` is 0 and `bonus.balance` is above 0, or mid-month when an earned bonus sits above the monthly allowance (shown as "+N bonus").
- **The order matches spending:** monthly credits are spent first.
- **Before unlocking** (`monthly.unlocked: false`), only bonus credits exist, so show those.

**The frontend has NOT been told yet** (owner, 2026-10-04), so nobody starts work early. When the backend build is ready to merge, the credits session tells the credits frontend this rule.

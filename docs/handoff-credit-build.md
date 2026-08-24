# Handoff — the credit build (M5b)

**Status:** planned, nothing built. Eleven PRs, one ADR.
**Written:** 2026-08-24, from `FE-ui-guide/reviewUI/CreditUI.png` and
`all card variation-the credit state-the monetize idea.png` measured against the
codebase, `docs/edufurther-migration/` and the dev Bubble app.
**Companion:** `handoff-review-build.md`, whose pattern this follows. M5a shipped
2026-08-24.

Read this before opening the UI files. It is the record of what was decided and
why, so the decisions do not get re-derived, re-argued, or silently reversed.

---

## The one-line state

**M5 split in two; this is M5b.** M5a was reviews and has shipped. M5b is
credits: four tables, a consumption path through booking, two scheduled jobs,
and the ETL that carries ~1,200 opening balances across.

**Referrals are in scope**, reversing the 2026-08-18 scoping that put them out.
The earning model makes a qualifying invite the thing that unlocks the monthly
grant, so credits cannot ship without them.

---

## The earning model

Decided 2026-08-24. It supersedes the package's D9, which gates a flat 1 → 5 → 5
ladder behind a referral.

| Trigger | Qty | Source | Expiry | Producer |
|---|---|---|---|---|
| Onboarding completed | 1 | `profile_completed` | **never** | `user_onboarding.completed_at` |
| First qualifying invite | +2 | `referral_unlock` | end of month | `referrals.qualified_at` |
| Each month, once unlocked | 3 | `monthly_free` | end of month | grant job, 1st |
| Migrated at cutover | their balance | `opening_balance` | EOM of cutover month | ETL, uniform |
| Mentor missed **or** cancelled unavailable | 1 | `refund` | inherits semantics | sweep / cancel path |

A completed profile gets you started; **one** successful invite opens the floor
for the recurring grant. Gating on profile completion rather than signup is the
anti-farming property D9 was reaching for and did not have — signing up is free,
finishing a profile is work.

**The starter never expires.** Reverted to this on 2026-08-24 after briefly
deciding otherwise: it exists so somebody gets a feel for the platform, and
expiring it makes the reward depend on the day they happened to finish
onboarding — complete on the 28th and it lives three days.

**Steady state is 4, not 3** — the non-expiring starter plus the monthly three.

---

## What was measured, against what the package claims

| # | Finding | Consequence |
|---|---|---|
| 1 | **The reset model contradicts the legacy data outright.** The card says resets happen on the 1st; legacy renew dates land on **13 different days of the month**, and exactly **one user in 37** renews on the 1st | Migrating `bookingCreditRenewDate` as a value would give each user a personal reset date the card then contradicts on screen |
| 2 | **`bookingCredit-WFCode` explains why.** Populated on 27 of 43, it is Bubble's per-user *scheduled-workflow handle* — so legacy renewal was a workflow per user, and the date is only its last footprint. 16 users hold a stale date with no workflow behind it | The column is an artifact, not an entitlement. It does not migrate |
| 3 | The package agrees with the UI, not the data — lots expire **end of month**, which pairs with a grant on the 1st | The target model was always calendar-wide; only the legacy implementation was per-user |
| 4 | **Referrals do not exist in the legacy app.** No field on `user`, no type among the Data API's 16, no export file. Meanwhile **29 of 43 dev users sit at 5 credits** | Legacy granted monthly credits unconditionally. D9's gate is greenfield product design, not a migration |
| 5 | **`bookingCredit` is a string, and `''` is distinct from `'0'`** — five users empty, two zero. Distribution: 29×`'5'`, 5×`''`, 3×`'1'`, 3×`'2'`, 2×`'0'`, 1×`'4'` | `''` means never entered the credit system and earns a starter; `'0'` means spent down and earns nothing |
| 6 | **25 of the 36 users who would get a lot have a renew date already in the past.** Fourteen share `Jun 30, 2025 11:17 am` — one bulk workflow run | Moot under decision 4 below, which discards the date entirely |
| 7 | **The card's progress bar is a position marker, not a fill gauge.** One segment is filled, at the index equal to the balance — 5→segment 5, 3→segment 3, 1→segment 1, 0→segment 1 in red | The server publishes no bar. It publishes `balance` and `allowance`; the client draws it |

---

## Decisions

### Shape

1. **Four tables**: `credit_lots`, `credit_transactions`, `referrals`,
   `referral_unlocks`.

2. **Five sources and five reasons, each with a producer.** Sources:
   `profile_completed`, `referral_unlock`, `monthly_free`, `refund`,
   `opening_balance`. Reasons: `grant`, `session_booked`,
   `session_cancelled_refund`, `session_no_show_refund`, `lot_expired`. Declared
   `text` + `CHECK` via `str_enum()` (#100, #153).

   **Not shipped:** `purchase`, `promotional`, `admin_grant` — settled decision
   #21 names this exact enum as its cautionary example, because `credit_source`
   "contains `purchase` while payments are out of scope by decision #8". Also not
   shipped: **`session_no_show_forfeit`**, which reads as a transaction and is
   not one — the credit left the balance when the session was booked, and not
   refunding it is the *absence* of a row. A member for it invites a second
   debit for one session.

3. **`opening_balance` is a new source the package does not have.** Calling a
   migrated balance `monthly_free` would assert a cadence finding 2 shows the
   source never had.

4. **Opening lots expire end of the cutover month, uniformly.** The legacy date
   is discarded as a value. It is the only option under which the card's
   "Next reset date" tells the truth on day one.

5. **`referral_unlocks.unlocked_by_referral_id` is nullable**, where the package
   makes it `NOT NULL REFERENCES referrals(id)`. Migrated users are unlocked
   *without* a referral, and the alternative is a synthetic referral row per
   user — inventing an invite that never happened. This is the #82 shape:
   unlocked, and the reason may be absent.

6. **Migrated users are grandfathered as unlocked.** ~1,200 people have never
   invited anybody; without this, every migrated mentee's balance goes to zero at
   the first month end and stays there. A migration that silently switches off a
   benefit people currently have is the kind they notice in week one.

7. **Foreign keys `RESTRICT`, where the package says `CASCADE`.** ADR 0013:
   restrict where the child is evidence. A ledger whose rows vanish with the
   account cannot answer "I was charged for a session that never ran", which is
   the first of D8's four reasons for the ledger existing. Deletion propagation
   is application logic, explicit and tested — and "never `ON DELETE CASCADE` in
   a soft-delete system" is a standing rule.

8. **`credit_transactions` carries no `updated_at` and no trigger.** It is
   append-only, and `session_events` settled this shape under #82 — the package
   gives the column while calling the table append-only a few lines above.

9. **`unit_cost_cents` and `currency` are deferred.** D7 argues for shipping them
   at zero so the payments work stays additive; #21 governs, and adding a column
   later is cheap in a way an enum value is not. Nothing writes them.

### Behaviour

10. **Booking spends; the balance is a gate.** At zero the card's CTA is dead and
    the server refuses. Enforcement is race-safe by `pg_advisory_xact_lock` on
    the mentee (D10) — counting then inserting in separate statements is a
    check-time-of-use bug, and a double-click gets three sessions past a limit of
    two.

11. **A refund is a fresh lot, not a return to the original.** Both triggers are
    settled *after* the session, so the lot that paid may already be dead, and
    returning a credit to a dead lot refunds nothing.

    The objection — that a refunded credit can outlive the one spent — needs a
    **colluding mentor**, because both triggers are mentor-side actions the
    mentee cannot cause. The failure on the other side happens by calendar. The
    schema already assumed this: `credit_source` carries `refund`, and a *source*
    value only makes sense if refunds create lots.

12. **A refund inherits expiry semantics, never the date.** Never-expiring in,
    never-expiring out; otherwise end of the current month. Today that always
    resolves to EOM, but the other way round would refund a purchased credit as a
    perishable one, and D7 is blunt that expiring something somebody bought is a
    chargeback and in several jurisdictions unlawful.

13. **One refund per session, as a database constraint.** The sweep commits per
    batch and is scheduled; a cancel followed by a sweep, or a sweep that runs
    twice, would otherwise pay twice.

14. **The no-show refund predicate reads `session_participants`.**
    `attendance.outcome()` collapses both parties into `COMPLETED` or `NO_SHOW`,
    so `sessions.status` cannot say *who* missed. The per-party fact exists; it
    is one join deeper than the status column suggests.

15. **Mentors receive no monthly grant** — but the predicate is **"has mentee
    goals"**, not "is not a mentor". Authorization here is profile existence, so
    a dual-role user is both, and a negative predicate would silently stop them
    booking.

16. **The reset is a platform boundary, UTC.** Users span Lagos, Toronto and
    Berlin; a per-user local midnight gives the monthly job no single moment to
    run, and the card shows one date, which is only truthful if the boundary is
    shared.

17. **The expiry job writes the ledger; the read filters independently.** Both,
    pinned by a test. Then a missed night makes the ledger late rather than the
    balance wrong. Two representations of one rule is #8, so the pin is not
    optional.

18. **`/me` carries `balance`, `allowance`, `next_reset_at` and `state`.**
    `allowance` is `max(MONTHLY_ALLOWANCE, balance)` — a late refund can push a
    balance above the allowance, and the card would otherwise show "4 credits
    left" beside a bar that cannot draw 4. Verified safe: `UserRead` is returned
    by `/me` and nothing else.

19. **Four status bands**: 4–5 on track, 2–3 moderate, 1 low, 0 exhausted.
    Published as a state name; the copy stays front-end.

20. **A qualifying invite is signup plus verified email — deliberately
    temporary.** The target is *invitee completes their profile*, which is the
    same signal as the starter credit. Recorded as temporary because the accepted
    risk is real: a farmed invite now unlocks a **recurring** grant rather than a
    one-off. Tightening is cheap — `referrals.qualified_at` is already separate
    from `signed_up_at`, so it is a predicate change rather than a migration.
    **Review `referral_unlocks` growth before that becomes expensive.**

21. **"Top up your credits" ships as a link with no purchase behind it.**
    Payments stay out of scope (#8) and the provider is undecided (#11) —
    collections are African, payouts global, and the two halves may not share a
    provider.

---

## Confidence — what will bite

Assessed against the codebase on 2026-08-24, before any code was written.

### Blockers

**`profile_completed` has no producer.** `user_onboarding.completed_at` is
written by exactly one thing — `infra/etl/satellites.py`, the ETL. There is **no
onboarding-completion flow in the API at all**, so the starter credit has nothing
to fire it. This is why the sequence below builds that producer as its own PR
rather than assuming it.

**No invite UI exists.** Nothing tells a user they can invite, or that it unlocks
anything. The endpoints must exist regardless — M5a shipped the same way — but
the earning model is unreachable until the front end catches up.

### The regression surface

**Eleven test files create bookings**: `test_api_attendance`, `test_api_booking`,
`test_api_freebusy`, `test_api_meeting_provisioning`,
`test_api_mentee_attendance_rate`, `test_api_notifications`,
`test_api_reminder_callback`, `test_api_response_deadline`, `test_api_sessions`,
`test_api_session_transitions`, `test_idempotency`.

Once booking spends a credit, every one needs a funded mentee. **The fixture
lands in the same PR as the break**, not after it — otherwise one PR turns eleven
files red at once and the temptation is to fix them in a hurry.

### Two risks checked and found not to be real

| Feared | Actual |
|---|---|
| A retry double-spends | **No.** The idempotency reservation and the booking row are explicitly one transaction, and a replay returns the stored response without re-executing |
| The balance leaks via `/users/{id}` | **No.** `UserRead` is returned only by `/me` |

### New machinery with no precedent

**`pg_advisory_xact_lock` appears nowhere in this codebase.** A wrong lock is
invisible — it does not error, it simply fails to protect. Lock-key derivation
and transaction scope both need a test that runs two genuinely concurrent
bookings, not one asserting the lock was called.

### One subtlety that would pass every rejecting test

**A booking debit and its refund share a `session_id`.** An index guarding "one
refund per session" that keys on `session_id` alone rejects the refund it exists
to permit — and every rejecting test still passes. The accepting case is the one
that catches it.

---

## Sequence

Ordered so nothing builds on a producer that does not exist, and so each step
leaves the system coherent. All Tier 1: migrations, ETL, a booking-path change,
and something that behaves like money.

| # | PR | Migration |
|---|---|---|
| 1 | `credit_lots` + `credit_transactions`, both vocabularies | yes |
| 2 | `referrals` + `referral_unlocks`, nullable unlock reason | yes |
| 3 | Balance on `/me` — balance, allowance, next reset, state | no |
| 4 | **Onboarding completion producer** + the `profile_completed` grant | no |
| 5 | Invite endpoints + the `referral_unlock` grant | no |
| 6 | Consumption — spends, refuses at zero, advisory lock, **and the funded-mentee fixture** | no |
| 7 | Refunds — mentor cancel and mentor no-show | yes |
| 8 | Monthly grant job, 1st, mentees only | no |
| 9 | Expiry job, plus the read filter and the test pinning them | no |
| 10 | Opening-balance ETL | no |
| 11 | ADR — the divergences, in one record | no |

**Eleven PRs against M5a's six.** Worth deciding whether it ships as one phase or
splits after PR 6.

**One ADR, not several.** Seven divergences from a document ADR 0007 makes
canonical — the earning ladder, `opening_balance`, the missing refund reason, the
nullable unlock, `RESTRICT` over `CASCADE`, the absent `updated_at`, and the
deferred cost columns. ADR 0026 took the same shape for the review scale.

---

## Definition of Done

- Every `credit_source` and `credit_reason` value shipped **has a producer**,
  asserted by a test that fails when one is added without one
- **A balance never goes negative** — proven by two *actually concurrent*
  bookings against a balance of 1, not by asserting a lock was called
- **One refund per session** — a database constraint, not an application check,
  with the shared-`session_id` accepting case
- **The ETL is idempotent** — a second run produces identical rows
- Reconciliation: one opening lot per user with a non-zero `bookingCredit`, and
  the lot sum equals the legacy sum
- The `expires_at` read filter and the expiry job agree, pinned by a test (#8)
- A migrated user's `/me` shows a balance and a `next_reset_at` that agree with
  the card
- Watch every test fail first · no threshold lowered · `security-checker` run,
  because this is authorization over a spendable resource · full gate green

---

## Register — what is still open

| # | Question | Blocks |
|---|---|---|
| 1 | **No invite UI.** The mechanism the earning model turns on has no screen | reachability, not PR 5 |
| 2 | Production `bookingCredit` values as a Data-tab export — the same gate M5a's reviews had | PR 10's rehearsal |
| 3 | Does the expiry job's first run sweep migrated lots, or grandfather them? Carried from 2026-08-18 | PR 9 |
| 4 | Ships as one phase or splits after PR 6? | sequencing |

---

## Adjacent, and not this phase

**`make_user` is copy-pasted into ten integration test files.** That is
non-negotiable #8's shape, and #43 records this project having already been bitten
by exactly it — *"three private copies of one test double all carrying the same
omission."* PR 6 adds an eleventh reason to care, because a funded mentee is the
kind of default that wants one home. Worth its own cleanup; it is not a credit PR's
job.

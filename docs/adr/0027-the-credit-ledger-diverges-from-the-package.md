# 27. The credit ledger ships five sources, five reasons, and RESTRICT

Date: 2026-08-25

## Status

Accepted.

Diverges from `docs/edufurther-migration/`, which ADR 0007 makes canonical for
the target data model. `05_credits_reviews.sql` specifies seven `credit_source`
values, a `session_no_show_forfeit` reason, `ON DELETE CASCADE` on every foreign
key, an `updated_at` column on a table it calls append-only, and
`unit_cost_cents` / `currency` on every lot. None of those five survives. The
package is not edited here (ADR 0007); this record is the departure.

**Every divergence here is chosen, not forced.** Unlike ADR 0026, where an `int`
column could not hold `3.34`, each of these declarations would have worked. That
is the reason to record them: a future reader comparing the schema against the
canonical DDL will find five differences and no compiler error explaining any of
them.

Landed in the PR that implements it, per how-we-work rule 2 and the precedent of
ADR 0026, which was `Accepted` in #197 — M5a's *first* PR, not its last. The
credit handoff's sequence put this record at PR 11; that was wrong against the
project's own practice, and five of the seven divergences are implemented by the
tables this PR creates.

## Context

M5b builds credits: what a user holds, how they earn it, what spends it. PR 1 is
the shape — two tables and two closed vocabularies — landing before anything can
depend on them.

The canonical DDL was written as a brain dump (ADR 0007) ahead of any measurement
against the legacy app. Five of its decisions do not survive contact with either
the measured data or decisions this project has already taken.

## Decision

### 1. Five sources, not seven

Shipped: `profile_completed`, `referral_unlock`, `monthly_free`, `refund`,
`opening_balance`. Not shipped: `purchase`, `promotional`, `admin_grant`.

Settled decision #21 names `credit_source` as its own cautionary example, because
it "contains `purchase` while payments are out of scope by decision #8". A
vocabulary is not a wish list. A shipped member invites a writer, and the reason
to defer these three is simply that nothing writes one.

`opening_balance` is a member the package does not have, and it exists because
calling a migrated balance `monthly_free` would assert a cadence the source never
had. **The legacy renewal was a per-user scheduled workflow, not a monthly
grant** — `bookingCredit-WFCode` is populated on 27 of 43 dev users, and the
renew dates land on thirteen different days of the month with exactly one user in
37 renewing on the 1st.

### 2. No `session_no_show_forfeit`

The package carries it as a reason. It reads as a transaction and is not one: the
credit left the balance when the session was booked, and a mentee who does not
turn up simply gets nothing back. **The absence of a row is the whole record.**

A member for it would eventually be written by somebody reading the name as an
instruction, and the balance would be debited twice for one session.

### 3. Foreign keys `RESTRICT`, not `CASCADE`

ADR 0013: cascade where the child is meaningless without its parent, restrict
where it is evidence. A ledger whose rows vanish with the account cannot answer
*"I was charged for a session that never ran"*, which is the first of D8's four
reasons for the ledger existing at all. "Never `ON DELETE CASCADE` in a
soft-delete system" is a standing rule here.

Both tables are named in `RETAINED_ON_USER_DELETE`, so
`test_no_cascade_path_reaches_a_table_that_must_be_retained` goes red on the
first future migration that routes a cascade to them — which is the moment
somebody can still choose differently.

### 4. `credit_transactions` carries no `updated_at` and no trigger

The package gives the column while calling the table append-only a few lines
above. A row states what happened at a moment, and a fact that can be edited is
not a log. `session_events` and `mentor_status_events` already settled this shape
under #82.

**The omission is invisible, which is why it has its own test.** `CREATE TRIGGER`
does not validate the function body, so attaching `trg_set_updated_at` to a table
with no `updated_at` succeeds and raises only at the first `UPDATE` — which an
append-only table never receives. `alembic check` is blind to triggers, and no
other test issues an `UPDATE` here. `test_the_ledger_carries_no_updated_at_trigger`
asserts it directly, paired with an accepting case on `credit_lots` so a
migration that forgot *both* triggers cannot pass.

### 5. `unit_cost_cents` and `currency` deferred

D7 argues for shipping them at zero so the payments work stays additive. #21
governs: nothing writes them, and **a column is cheap to add later in a way an
enum member is not**. The asymmetry is the whole argument — PostgreSQL cannot
remove a value from a native enum at all, and while #100 moved these to `text` +
`CHECK`, a shipped member still invites a writer.

### 6. The lot's owner and the movement's owner cannot disagree

**An addition, not a divergence** — the package has nothing here, which is the
problem. `credit_transactions.user_id` is denormalised from its lot so that every
"this user's ledger" read is one table. The cost of that copy is that the two can
disagree, and **nothing above the database would notice**: a lot-scoped balance
and a transaction-scoped balance would each look correctly filtered and return
different numbers, while a global reconciliation of "lot sum equals ledger sum"
passes.

So `credit_lots` carries `UNIQUE (user_id, id)` — redundant against its primary
key, and existing solely to be referenceable — and `credit_transactions` points a
**composite** foreign key at it rather than a single-column key on
`credit_lot_id`.

This project has met this exact defect once already and wrote the rule down at
`session_types.conferencing_option_id`: *"a single-column key is satisfied by
**any** option row, including another mentor's."* Substitute one noun. The
enabling `UNIQUE (user_id, id)` on `mentor_conferencing_options` is the same
pair, for the same reason — this is that pattern, not a new one, and
non-negotiable #10's second sentence is what licenses it.

Both columns here are `NOT NULL`, so `MATCH SIMPLE` never skips the check —
unlike the precedent, where a nullable option legitimately means "use my
default".

Found by `security-checker` before any writer existed, which is why it is two
lines rather than a backfill and a validate.

### 7. `referrals` carries no `status` column

The canonical DDL declares `referral_status` — `sent`, `signed_up`,
`qualified`, `expired`, `rejected` — and a column holding it **beside**
`signed_up_at` and `qualified_at`.

Status is entirely derivable from those two timestamps. There is no state it can
express that they cannot, so carrying both is one rule in two representations —
non-negotiable #8, which this project calls a defect rather than a style
question. The drift it invites is concrete: a row reading `qualified` beside a
null `qualified_at`, and nothing to say which is right.

Two of the five values have no producer either (#21). No type is created, so
none has to be dropped later. Revocation, if it ships, is a nullable
`revoked_at` — additive, and genuinely underivable.

### 8. `referral_unlocks` gets a surrogate id

The package makes `user_id` the primary key. ADR 0015 and non-negotiable #10
admit no natural keys — and #10 says what to do instead: *"an invariant a
natural or composite key would have carried is re-declared as `UNIQUE`."* The
package's own argument for the natural key, that it makes double-unlocking
structurally impossible, is preserved exactly by `UNIQUE (user_id)`.

That rule was overridden twice before with every gate green, which is why
`test_every_table_has_a_generated_surrogate_primary_key` walks the live schema
rather than a list somebody maintains.

### 9. `unlocked_by_referral_id` is nullable

The package makes it `NOT NULL REFERENCES referrals(id)`. ~1,200 migrated users
are grandfathered as unlocked and none of them ever invited anybody. The two
alternatives were a synthetic referral row per user — inventing an invite that
never happened, in a table whose entire purpose is evidence — or letting every
migrated mentee's balance fall to zero at the first month end, which is a
migration that silently switches off a benefit people currently have.

This is the #82 shape: unlocked, and the reason may be absent.

### 10. `invitee_email` is `text`, not `citext`

`users.email` settled this. `citext` was adopted there first and reversed,
because a case-insensitive type is a second mechanism for an invariant the
boundary already holds — and it was the only object in the chain whose behaviour
on Supabase was unverified. A lowercase `CHECK` fails loudly where the type
would have silently accepted.

### 11. Onboarding completion is a route this project had to invent

**Not a divergence — a gap.** The earning model's first rung reads
`user_onboarding.completed_at`, and before PR 4 that column had exactly one
writer: `infra/etl/satellites.py`. There was no onboarding-completion flow in
the API at all, so the starter credit had nothing to fire it.

`POST /api/v1/me/onboarding/completion` is that producer. A sub-resource rather
than a flag, because `PATCH {"completed": false}` is a transition that does not
exist and a route which appears to allow it is one somebody eventually calls.

**The bar is row existence, and that is deliberately temporary.** The starter is
granted for finishing a profile rather than for signing up — signing up is free,
finishing one is work — and that asymmetry is the whole anti-farming property.
But `user_profiles` and `mentee_goals` have **no required columns**: everything
but `user_id` is nullable, so "complete" cannot mean "filled in" today without
this route inventing a field policy the rest of the codebase does not have.

It means a profile row plus a role-appropriate profile — a mentee goal or a
mentor profile. Role-appropriate rather than mentee-only, because gating on the
goal alone refuses a mentor who did the mentor half first.

This is the same shape settled decision 20 takes for a qualifying invite, and it
is recorded the same way: **tightening it is a predicate change in
`domain/onboarding.py`, not a migration.** When the onboarding flow gains a
defined set of required steps, that function is the one place that learns them.

The accepted risk, named rather than waved at: an attacker can post two
nearly-empty rows and collect one non-expiring credit — once, per account that
also passes email verification. Against the referral unlock, which opens a
*recurring* grant, which is why that gate is the stricter of the two.

**The grant is idempotent by construction.** `grant_starter` inserts with
`ON CONFLICT DO NOTHING` against the partial unique index rather than reading
first. A read-then-write is a check-time-of-use race — two concurrent
completions both see no starter and both insert — and letting the database
arbitrate means the loser learns it lost. Watched failing: with the conflict
clause removed, a second call raises instead of answering 200.

**201 and 200 are drawn from whether a lot was created**, not from whether the
onboarding row already existed. A migrated user whose completion the ETL wrote
but who never received a credit gets both the credit and a 201, which is the
honest answer; keying off the row would answer 200 and silently skip the grant.

### 12. A qualifying invite is the invitee finishing their profile

**Amends settled decision 20**, which said "signup plus a verified email" and
recorded itself as deliberately temporary, naming *invitee completes their
profile* as the target.

It could not have shipped as written. `users.email_verified_at` is written by
exactly one thing — `domain/transform/identity.py`, the ETL, for migrated users
— and `TokenClaims` extracts only `subject` and `email` from the Supabase token,
never `email_verified`. For any new user that column stays null forever, so
`qualified_at` would never be set and **the unlock gate could never open**: a
recurring benefit gated behind a condition nothing in the system can satisfy.

Three ways out were weighed — read `email_verified` from the token and write the
column; drop the verification half; or move to onboarding completion. The third
was chosen, and it is barely a departure: it is the target decision 20 already
named, and PR 4 built the producer that made it cheap. It is also **strictly
stronger** than what it replaces, since finishing a profile is work and clicking
a verification link is not.

Confirmed by the owner on 2026-08-25 rather than taken locally, because it
changes when somebody earns a *recurring* benefit.

### 13. `signed_up_at` records the claim, not the signup

**This service cannot observe a signup.** `users` rows are created by
`provisioning_store` (the provisioning CLI) and by the ETL; there is no
registration endpoint here to notice an arrival. So the invitee presents their
code once authenticated — `POST /api/v1/me/referrals/claim` — and that is the
moment recorded.

The package's column name is kept, because a later Supabase webhook would fill
exactly this column with exactly this meaning. But the gap between the name and
what the service actually witnessed is real, and it is written into the model
docstring so nobody reasons about signup funnels from it.

### 14. The invite code is unique per referral, and is a bearer token

The package indexes `code` non-uniquely, which implies a code per *referrer*. A
per-referrer code cannot tell an arrival which invite it answered, and
`invitee_email` cannot stand in because a shared link has no addressee — so
`uq_referrals_code` makes it one row per invite.

Generated with `secrets.token_urlsafe(16)`: 128 bits, URL-safe because it
travels in a link. **Anybody holding a code can attach themselves to the invite
it names**, so being infeasible to guess is its only protection. `secrets`
rather than `random`, which is seeded predictably and documented as unsuitable.

`referrer_id <> invitee_user_id` **is** a rule the package does not have, and it
is here because this gate opens a *recurring* grant rather than a one-off: the
cheapest possible farm is one address referring itself.

`UNIQUE (referrer_id, invitee_email)` is **not** — it is the package's own rule
at `05_credits_reviews.sql:137`, carried unchanged. An earlier draft of this
record claimed it as a departure, which was simply wrong; the scoping argument
for it stands, but the credit belongs to the package.

### Confirmation

How you would know this is being honoured, and what nothing checks.

| claim | what checks it |
|---|---|
| five sources, five reasons, no more | `test_every_source_shipped_has_a_producer_in_this_phase` and its reason twin assert the **exact** set, so a member added without a producer goes red |
| `purchase` never ships quietly | `test_purchase_and_promotional_are_absent` names all three by hand, because a set comparison alone reads as arithmetic rather than as the decision #21 records |
| the database refuses what the enum omits | `test_the_source_vocabulary_is_closed` writes `purchase` straight to the column; `test_the_reason_vocabulary_is_closed` writes `session_no_show_forfeit` |
| the `CHECK`s accept what they should | `test_every_shipped_source_is_accepted` and `test_every_shipped_reason_is_accepted` — a constraint that refuses everything also refuses garbage |
| the model and the migration cannot drift | `test_every_converted_enum_has_a_check_naming_its_values` compares the migration's hand-written literal against the class in `pg_constraint` |
| **one starter, without blocking monthly grants** | `test_a_user_gets_one_starter_ever` *and* `test_the_starter_index_does_not_constrain_other_sources` — the second is the one that catches a predicate-less index |
| **a debit and its refund may share a session** | `test_the_debit_and_its_refund_share_a_session`. Every *rejecting* test passes against an index keyed on `session_id` alone; only this accepting case fails |
| one refund per session, structurally | `test_a_session_is_refunded_once`, with `test_refunds_of_different_sessions_do_not_collide` as the accepting half |
| the ledger survives a user delete | `test_a_user_holding_credits_cannot_be_hard_deleted`, against a holder who owns a lot **and nothing else** — the fixture's mentee is referenced by `sessions`, so deleting them reports on the session FK while appearing to test this one |
| no future cascade reaches the ledger | both tables are in `RETAINED_ON_USER_DELETE`, and the check walks the transitive closure rather than one edge |
| **a movement cannot name someone else's lot** | `test_a_movement_cannot_name_a_lot_belonging_to_someone_else`, with `test_a_movement_against_your_own_lot_is_accepted` as the accepting half. Watched failing against a single-column key before the composite landed — a transposed composite rejects both cases just as loudly, so the accepting half is what distinguishes a working constraint from one that refuses everything |
| an unlock may name no referral | `test_an_unlock_may_name_no_referral` — the accepting case the migration depends on; a `NOT NULL` here passes every rejecting test and makes cutover impossible |
| two referrers may invite one person | `test_two_referrers_may_invite_the_same_person`. Scoped to `invitee_email` alone the programme becomes a race to invite, and the rejecting test passes either way |
| nobody refers themselves | `test_nobody_may_refer_themselves`, with `test_a_referral_may_name_a_real_invitee` as the accepting half |
| qualifying implies signing up | `test_qualifying_requires_having_signed_up`, plus both legal orderings |
| one unlock per user, ever | `test_a_user_unlocks_once_ever` — the `UNIQUE` carrying what the package's natural key carried |
| the starter cannot be farmed by signing up | `test_a_bare_account_is_refused` at the boundary, plus every incomplete combination enumerated in `test_onboarding_domain.py` — a predicate written with `or` where it meant `and` passes all three positive cases |
| a refusal grants nothing | `test_a_refusal_grants_nothing` asserts the empty ledger, not just the 409 |
| **a retry pays nothing** | `test_a_second_call_grants_nothing`, asserted on the *balance* rather than the status code — a route answering 200 while quietly inserting a second lot passes a status assertion. Watched failing with the conflict clause removed |
| a grant always writes its ledger row | `test_the_grant_writes_a_ledger_row` — a writer that created the lot and forgot the entry passes every balance assertion in that file |
| a migrated completion still pays | `test_a_user_the_etl_completed_still_gets_their_credit` |
| an invite pays nothing until the invitee finishes | `test_claiming_alone_pays_nothing`, with `test_finishing_onboarding_pays_the_referrer` as the accepting half — the separation is the abuse boundary |
| **a second qualifying invitee pays nothing more** | `test_a_second_qualifying_invitee_pays_nothing_more`, enforced by `uq_referral_unlocks_user_id` rather than a read-then-write, which two invitees finishing at once would lose |
| the second invite is still recorded as qualified | `test_the_second_invite_is_still_marked_qualified` — it genuinely qualified, even though the floor it would have opened is already open |
| **a repeat does not move `qualified_at`** | `test_the_invitee_finishing_twice_does_not_move_qualified_at`. An earlier version asserted the *balance* and passed with the predicate removed — the unlock's `UNIQUE` refuses the second row either way. Only the date assertion reaches the rule |
| nobody claims their own invite | `test_claiming_your_own_invite_is_refused` — the CHECK is the guarantee, this is what makes it a 409 rather than a 500 |
| a claim is idempotent | `test_claiming_twice_yourself_is_idempotent`; the front end holds the code across sign-up and a retry must not read as an error |
| **the append-only table has no trigger** | `test_the_ledger_carries_no_updated_at_trigger`, paired with `test_the_lots_table_does_carry_one`. Nothing else can see this: `CREATE TRIGGER` does not validate the function body, `alembic check` is blind to triggers, and no test issues an `UPDATE` here |
| quantities cannot go incoherent | four CHECK tests, each with its accepting boundary — `granted == remaining` and `remaining == 0` are both legal states |

**What nothing checks.** That the *five* sources are the right five — a sixth
with a genuine producer would be a product decision, and the tests would simply
be updated alongside it. That `opening_balance` lots carry the right expiry;
that is PR 10's reconciliation, and this PR ships no writer. And whether the
expiry job's first run should sweep migrated lots or grandfather them, which is
open and belongs to PR 9.

## Consequences

**The two partial indexes are the load-bearing part of this PR, not the tables.**
`uq_credit_lots_one_starter_per_user` is keyed on `user_id` *with* a predicate on
`source`; on `user_id` alone it would stop a user ever receiving a second monthly
grant. `uq_credit_transactions_one_refund_per_session` is keyed on `session_id`
*with* a predicate naming the two refund reasons, because **a booking debit and
its refund share a `session_id`** — keyed on the column alone it would reject the
refund it exists to permit, and every *rejecting* test would still pass.

**A refund is a fresh lot, not a return to the original**, and it inherits expiry
*semantics* rather than the date. Both refund triggers are settled after the
session has run, so the lot that paid may already be dead. Never-expiring in,
never-expiring out: today that is only the starter, but once payments land it is
every purchased credit, and expiring something somebody bought is a chargeback
and in several jurisdictions unlawful.

**What this record does not decide.** Whether the expiry job's first run sweeps
migrated lots or grandfathers them is open and belongs to PR 9. Whether the
opening lots carry the legacy date is decided — they do not, uniformly end of the
cutover month — but the reconciliation that proves it is PR 10's.

**The vocabularies are the first declared after #100 finished**, so they are born
as `text` + `CHECK` rather than converted into it. Adding a member later is a
constraint swap in one migration, not a permanent `ALTER TYPE ... ADD VALUE`.

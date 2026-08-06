# 18. Auth provisioning is eager, idempotent, and silent

Date: 2026-08-06

## Status

Accepted

## Context

ADR 0014 made `users.id` ours and `auth_id` a nullable column, and in doing so
turned auth provisioning into a step that could happen almost anywhere:

> auth provisioning becomes a separate, re-runnable step that can happen before,
> during or after the load — **or lazily, at each user's first login**.

That freedom was the point of 0014, and it left three questions unanswered. Its
own Confirmation names the sharpest one:

> **M1c needs a reconciliation query**, and it does not exist yet. This is the
> weakest part of this record.

and its Open questions ask **"what reconciles `auth_id` against Supabase, and how
an orphan is surfaced"**, deferring it to M1c.

M1c is done. Building the provisioning step forced all three: *when* it runs,
*what recovers it when it fails halfway*, and *who is told*. Roughly 1,200
migrated users acquire a Supabase account during a cutover freeze, and none of
those questions has a reversible answer — an auth account, once created, cannot
be un-created in any sense a user would recognise.

A fourth question arrived from the same work and is answered here rather than in
a separate record, because it is the same story: **who grants the first
administrator**, when there is no administrator yet to approve it.

## Decision

**1. Provisioning is eager: every migrated user gets an account before cutover,
not at their first login.**

The argument that decides it is not throughput, it is provability. An eager run
executes end to end against a rehearsal database and reports a count, so it can
be *known correct before the freeze*. Lazy linking surfaces a defect one user at
a time, after go-live, which is the worst available moment to discover that
account linking does not work — and the users who find it are the ones who came
back.

**2. Idempotent re-run replaces rollback, because rollback does not exist.**

There is no undo for creating 1,200 auth accounts. Rather than pretend otherwise,
every mode is safe to run again:

- A row that already holds an `auth_id` is skipped **without consulting Supabase
  at all**, so re-running a provisioned population costs zero API calls.
- An account created by a run that died before recording it is found by address
  and linked. This is the *normal* resume path, not an anomaly.
- `--link-migrated` commits per user, so work already done stays done.
- The `UPDATE` carries `auth_id IS NULL`, so a second concurrent run is a no-op
  on rows the first already linked rather than a clobber that orphans a working
  account.

The recovery plan in the runbook is therefore "run it again", not
`alembic downgrade` and not a restore.

**3. `--verify` is the reconciliation 0014 asked for — in one direction.**

It walks every live user holding an `auth_id` and asks Supabase whether that
account exists, reporting an id Supabase does not have and an address the two
systems disagree on.

**It covers `users → Supabase` and not the reverse.** An auth account created by
a `--create` run that died before its `INSERT` is invisible to it, and is
recovered only when an operator re-runs `--create` for the same address. That is
tolerable at 1,200 rows under supervision and it is stated here rather than left
for someone to discover, because this record's ancestor already lost points for
claiming coverage it did not have.

**4. Provisioning notifies nobody, and that is a product decision.**

Supabase offers three ways to bring a user into existence and two of them send
email. Provisioning uses only `POST /auth/v1/admin/users` with
`email_confirm=True`.

The reasoning is not technical. These 1,200 people have not been told the
platform moved. An unexpected message from a system they have never heard of,
arriving during a freeze, reads as phishing — and 1,200 of them cannot be
recalled. Migrated users verified their address in Bubble years ago, so asking
again would be asking them to re-prove something already migrated.

A future welcome email is a *communications* decision with a sender, a schedule
and a message, made by someone who owns that. It is not a side effect of the
provisioning endpoint chosen.

### The bootstrap grant

**`--grant-admin` has no authorization check of its own. The authority is
possession of the database credentials, and only those.**

There is no first administrator to approve the first administrator, so the chain
has to start out of band. The mode never contacts Supabase and needs no
service-role key.

**The service-role key is deliberately not a second factor here.** An earlier
draft of the tier-2 row claimed it was, and that was wrong in the direction that
matters: anyone holding the DSN can `INSERT INTO admin_users` directly, so
requiring the key in the CLI would have documented a control the system does not
have. This project has already logged one incident of a record overstating a
control, and an overstated control is the harder error to notice — nobody audits
a safeguard for being too strong.

Every CLI grant lands with `granted_by` null. That is the honest record of a
grant made out of band, and the same value every migrated grant carries, because
the legacy option set recorded *that* someone was an admin and never *who* made
them one. A synthetic "system" actor would look like knowledge we do not have, in
the one table whose entire purpose is being auditable.

### Rejected alternatives

**Lazy linking at first login.** Genuinely attractive: no freeze-window work, no
accounts created for users who never return, and cost proportional to actual use.
Rejected because it cannot be rehearsed. The first real execution of the linking
path would be a stranger's login attempt, and a defect there is invisible until
someone reports being unable to sign in — by which time the population affected
is unknown and the operator is debugging under pressure. Eager provisioning
converts that into a batch that either completes or prints a list of addresses.

**Invitation email as the provisioning mechanism.** `POST /auth/v1/invite`
creates the account *and* tells the user, which sounds like one step instead of
two. Rejected because it couples an irreversible technical action to an
un-recallable communication, and because the invitation would arrive before
anyone had explained what it was. It also cannot be rehearsed against production
addresses, which removes the property that decided point 1.

**A hybrid — provision eagerly, link lazily.** Create the accounts before cutover
but write `auth_id` at first login. Rejected because it keeps the irreversible
half and defers the verifiable half: the accounts exist, so the risk has been
taken, but nothing proves the link works until users start arriving. It is the
worst column of both.

## Consequences

**The cutover step is a batch with a count, not a behaviour that emerges.** An
operator can run it, read four numbers that sum to the population, and act on a
list of addresses. That is the property lazy linking cannot offer at any price.

**"Run it again" is a real answer.** Every failure mode above resolves by
re-running, which is why the idempotence is load-bearing rather than tidy. It is
also why nothing retries a `create` internally: a timed-out create may have
succeeded, and a blind retry would either make a second account or lean on the
`email_exists` branch to clean up after it. Re-running the whole command is the
safe recovery precisely because each action is idempotent.

**`updated_at` survives.** Provisioning writes to `users`, and `users.updated_at`
carries Bubble's Modified Date, so the link is written with
`trg_set_updated_at` held off. Without that, provisioning would silently rewrite
1,200 modification dates to the import clock one day after M1c preserved them,
with nothing to restore from.

**One direction of the reconciliation gap 0014 named is now closed**, and the
other is named. That is a smaller claim than "0014's weakest point is fixed", and
it is the true one.

### Confirmation

- **Mechanical:** `test_a_second_run_costs_nothing` asserts a re-run makes *zero*
  API calls and leaves the database byte-identical — the property the whole
  recovery story rests on.
- **Mechanical:** `test_an_account_that_already_exists_is_linked_rather_than_recreated`
  and `test_an_address_that_is_a_substring_of_another_is_still_found` cover the
  resume path, including the case where the provider's substring search answers
  with a neighbouring address.
- **Mechanical:** `test_no_endpoint_that_sends_email_is_ever_called` drives every
  method of the Admin API client and asserts none reaches an invite endpoint.
  Proved by mutation — pointing the client at one and confirming that test, and
  only that test, goes red.
- **Mechanical:** `test_verify_catches_an_auth_id_supabase_does_not_have` and
  `test_verify_reports_an_address_the_two_systems_disagree_on` cover both
  failures `--verify` is for.
- **Mechanical:** `test_provisioning_does_not_rewrite_the_migrated_modified_date`
  asserts a known past timestamp survives a run.
- **Mechanical:** `test_a_grant_is_recorded_with_no_granter`,
  `test_granting_the_same_role_twice_is_a_no_op` and
  `test_a_revoked_role_can_be_granted_again` cover the bootstrap grant, including
  that a revoked historical row neither blocks a re-grant nor disappears.
- **Not mechanical:** nothing proves the `Supabase → users` direction, because
  nothing looks. See point 3 and the open question below.
- **Not mechanical:** nothing prevents a future caller from reaching an invite
  endpoint. `INVITE_ENDPOINTS` exists so a test can assert the absence, which is
  weaker than being unable to and the strongest guarantee available.
- **Not mechanical:** the no-notification decision holds only as long as nobody
  adds a notification elsewhere. It is a decision, recorded here; there is no
  check that could enforce it.

### Open questions

- **What closes the `Supabase → users` direction.** An account with no
  corresponding row is currently invisible. A `--verify --orphans` pass listing
  Supabase accounts no live user references would close it; nothing needs it
  until a `--create` run actually fails that way.
- **Whether provisioning scales past one freeze window.** ~2,400 sequential calls
  is fine at 1,200 users and does not hold at ten times that. The rate-limit
  retry is in place; concurrency is not, and should not be added before it is
  needed.
- **What happens to the bootstrap rule once an in-product admin surface exists.**
  Grants made through it would carry a real `granted_by`, and this becomes the
  bootstrap path only. Nothing forces that split today.
- **Who owns `email` once both systems hold one**, inherited unanswered from
  0014. `--verify` now *reports* a divergence; it still does not say which side
  wins, and the first-login flow that has to answer it is not built.

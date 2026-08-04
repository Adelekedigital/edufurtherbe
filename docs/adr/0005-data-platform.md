# 5. Use Supabase for Postgres, auth and storage — as Postgres, not as a backend

Date: 2026-08-03

## Status

Accepted. The decision itself stands; one of its open questions is superseded:

- **"Whether Supabase Auth's magic-link flow satisfies decision #7 exactly"** —
  superseded by ADR 0009, which makes a 6-digit email code the default and offers
  the link as a choice. The question is **moot rather than answered**: the
  magic-link flow is no longer the mechanism, so whether it satisfies decision #7
  no longer decides anything. The equivalent question for the code flow is open in
  ADR 0009, which records that first login cannot be tested end to end until M1
  builds it. Nothing here has been verified against the 1,200 migrated users.

## Context

The cutover needs three things that do not exist yet: a Postgres database, an
authentication mechanism, and somewhere to put files.

**Authentication is constrained by decision #7.** Bubble password hashes are not
exportable, so identity does not migrate. Every one of the 1,200 users
(`docs/bubble-data-model.md`) matches on email and completes a magic link or
reset on first login. Whatever we choose must do passwordless email login well,
because that is the first thing every migrated user will touch.

**File storage is constrained by ADR 0002.** Bubble's file and image fields are
URLs on its `appforest_uf` bucket, and they stay reachable only while the Bubble
app is paid for. Migrated files have to land somewhere we control, during the
freeze, or they are lost when the legacy app is switched off.

**The architecture constrains what a database vendor may do.** `domain/` imports
no framework and no layer except `core/`, enforced by `scripts/check_layers.py`.
Object-level authorization is scoped in the query on every read and write path.
Both rules assume authorization and business logic live in Python, where the
layer check can see them and unit tests can reach them without a database.

**The team is small and the deadline is the cutover.** Integrating three separate
vendors before the freeze is three integrations that can go wrong, at a time when
the migration itself is the risk worth spending attention on.

Alternatives considered were Neon for Postgres with separate vendors for auth and
storage, and self-managed Postgres.

## Decision

**Supabase provides Postgres, authentication and file storage for this phase.**

**It is used as Postgres, not as a backend.** Specifically:

- **No PostgREST.** The API surface is this FastAPI service. Nothing else serves
  application traffic from the database.
- **No row-level security as application logic.** RLS may be used as defence in
  depth. It may not be the place where a product rule about who can see what is
  expressed.
- Vendor SDKs stay inside `infra/`, as house convention already requires;
  `domain/` expresses the need as a Protocol in `domain/ports.py`.

The reasoning behind the RLS constraint is worth stating, because it is the rule
most likely to be argued with. A policy in the database is invisible to
`scripts/check_layers.py`, unreachable by a unit test that does not stand up a
database, and unversioned alongside the Python that appears to implement the same
rule. Authorization expressed in two places drifts, and the copy that drifts is
the one nobody is reading. Non-negotiable #5 requires the scope to be in the
query; that is a statement about *where the logic lives*, not merely about
whether rows are filtered.

### Rejected alternatives

**Neon plus separate auth and storage vendors.** Neon's database branching is
genuinely better suited to the expand/contract migration workflow — a branch per
migration is a real advantage and we are giving it up. It was rejected because it
solves one of the three problems and leaves two vendor selections to make and
integrate before the freeze.

**Self-managed Postgres.** Full control, no vendor, cheapest at scale. Rejected
because backup verification, point-in-time recovery and patching are ongoing
attention from a team whose attention is the scarce resource this quarter. The
strongest argument for it is that it is the only option with no exit problem at
all.

**Best-of-breed per concern.** Would produce a better answer to each individual
question. Rejected on integration count and on the number of failure domains a
small team can hold in its head during a migration.

## Consequences

One vendor, one set of credentials, one place to look during the cutover.
Passwordless email login arrives without being built, which removes work from the
critical path of a migration that has already committed to every user
re-authenticating.

The database is ordinary Postgres. Migrating it is a dump and a restore, so the
*data* is not locked in.

**The auth records are the sticky part, and they are stickier than the data.**
User identities, sessions and the magic-link flow are Supabase-shaped. Moving
them later is materially harder than moving tables, and it would land on the
users as a second forced re-authentication. This is the real lock-in and it
should be understood as the price of the bundle rather than assumed away.

**Three responsibilities share one failure domain.** A Supabase incident is
simultaneously a database outage, a login outage and a file outage. Separate
vendors would have made these independent. At current scale the trade is
acceptable; it becomes less so as usage grows.

**Point-in-time recovery is a paid add-on.** On daily snapshots alone, a restore
can lose up to a day. For the user table during and immediately after the
migration, that is a day of re-onboarding. The plan tier is therefore a
migration-window decision, not a cost decision.

**Connection pooling needs deliberate configuration.** Supabase steers clients
toward its pooler, and transaction-mode pooling changes the behaviour of prepared
statements and session state. Whatever pooling mode the application uses must be
chosen explicitly and tested, not inherited from a default connection string.

**If Nango is ever self-hosted** (ADR 0004), it gets its own Supabase project. It
runs migrations on startup and stores encrypted OAuth credentials; neither
belongs in the database holding bookings and sessions.

### Confirmation

- **Mechanical:** `scripts/check_layers.py` fails if a Supabase SDK is imported
  outside `infra/`.
- **Mechanical:** no PostgREST usage is observable as the absence of any client
  pointing at the REST endpoint; introducing one would be a visible dependency
  and configuration addition.
- **Not mechanical:** nothing prevents an RLS policy being written that encodes a
  product rule. There is no check that can distinguish defence-in-depth from
  application logic, because the difference is intent. This is enforced by review
  against this record, and it is the weakest confirmation in it.
- **Not mechanical:** nothing verifies that a restore actually works, or that the
  backup tier matches what the migration window needs. A restore that has never
  been performed is an assumption.

### Open questions

- **Which Supabase plan**, decided against the point-in-time-recovery need during
  the migration window rather than against monthly cost.
- **Pooling mode** for the application's database connections.
- **Whether Supabase Auth's magic-link flow satisfies decision #7 exactly**, in
  particular for the 1,200 migrated users matching on email. Not yet tested.

# 12. Foreign-key deletion policy for user-owned rows

Date: 2026-08-05

## Status

Accepted

> Accepted directly rather than landing as `Proposed` first, following ADR 0011
> rather than 0008 and 0009. The distinction is whether a decision **blocks** work
> or is **implemented by** it: 0008 and 0009 gated phases that could not start
> until they were settled, so each earned a review of its own. This record is
> implemented by the migration in the same pull request, and reviewing the rule
> apart from the `ON DELETE` clauses that express it would review it twice and
> test it never. The README describes only the first flow; that gap is noted
> there.

## Context

M1 creates the first tables that hang off `users`. Every one of them needs an
`ON DELETE` rule, and **the migration package gives two contradictory answers.**

`00_HANDOFF.md`, principle 4, is absolute:

> Never `ON DELETE CASCADE` in a soft-delete system. Cascade is a hard-delete
> mechanism. Deletion propagation is application logic, explicit and tested.

`01_identity.sql` then puts `ON DELETE CASCADE` on **every** child of `users`
without exception — `user_profiles`, `auth_identities`, `auth_codes`,
`user_onboarding`, `user_legal_consents`, `admin_users`, `user_languages` and
`account_deletion_requests`. The DDL contradicts the principle stated four files
earlier, and neither text acknowledges the other.

ADR 0007 makes the package canonical and ADR 0011 makes its DDL authoritative
over its prose, which would settle this in favour of blanket cascade — except
that the conflict is *within* the package, so the tiebreak has to come from
somewhere else.

**It comes from what deletion actually means here.** `users.deleted_at` is a soft
delete, and D31 specifies user-chosen deletion where "delete completely" means
**anonymise**, not remove. `01_identity.sql`'s own comment sets out the plan:
identifiers are overwritten, `auth_identities` and `auth_codes` are hard-deleted,
and `sessions`, `reviews` and `credit_transactions` are "RETAINED, now anonymous"
because destroying them would corrupt every mentor's completion count and rating.

That has a consequence the package never draws: **under anonymisation, a `users`
row is never hard-deleted at all.** So a cascade on its children is a mechanism
that, in the designed system, can never fire. It is neither the danger principle 4
describes nor the convenience the DDL implies — it is dormant, and the only
things that will ever trigger it are a test fixture, a cleanup script, or a future
erasure job nobody has written yet.

The question is therefore not "cascade or not" in the abstract. It is: when
something eventually issues `DELETE FROM users` by accident, what should happen?

## Decision

**Cascade where the child is meaningless without its parent and records no
auditable fact. Restrict where the child is legal proof or a privilege trail.**

Every foreign key M1 creates, with its rule:

| Child | Column | Parent | Rule |
|---|---|---|---|
| `user_profiles` | `user_id` | `users` | **CASCADE** |
| `user_onboarding` | `user_id` | `users` | **CASCADE** |
| `user_languages` | `user_id` | `users` | **CASCADE** |
| `auth_identities` | `user_id` | `users` | **CASCADE** |
| `admin_users` | `user_id` | `users` | **RESTRICT** |
| `admin_users` | `granted_by` | `users` | **RESTRICT** (nullable) |
| `admin_users` | `revoked_by` | `users` | **RESTRICT** (nullable) |
| `user_legal_consents` | `user_id` | `users` | **RESTRICT** |
| `user_legal_consents` | `legal_document_id` | `legal_documents` | **RESTRICT** |
| `user_profiles` | `origin_country_code` | `countries` | **RESTRICT** |
| `user_profiles` | `current_country_code` | `countries` | **RESTRICT** |
| `user_languages` | `language_code` | `languages` | **RESTRICT** |

`RESTRICT` is spelled out rather than left to PostgreSQL's `NO ACTION` default.
The two differ only in deferrability, which nothing here uses, and an explicit
clause is the difference between a decision and an omission that happens to
behave correctly.

The four cascades are the same set ADR 0009's anonymisation plan already treats as
disposable: `auth_identities` it hard-deletes outright, and profile, onboarding
and language rows carry no fact that survives the person they describe.

**`admin_users` and `user_legal_consents` are the exceptions and the reason this
record exists.** A consent row is evidence that a specific human accepted a
specific document version — the kind of thing that is worthless if it can be
destroyed by a cascade nobody was thinking about, and impossible to reconstruct
afterwards. An `admin_users` row is the audit trail for who held elevated access
and who granted it; the package's own justification for the table is that the
legacy design made admin **un-revocable and unauditable**, which a cascade would
partly reinstate.

**The rule generalises to M2–M6** and is meant to be applied without reopening
this record: 1:1 extensions and junction tables cascade; anything answering "who
did what, and when" restricts.

### Rejected alternatives

**Blanket `CASCADE`, as `01_identity.sql` writes it.** Matches the DDL, needs no
argument, and is what a reader copying the package would produce. Rejected because
it makes `DELETE FROM users` silently destroy consent evidence and the admin audit
trail — and it does so most plausibly in exactly the contexts where nobody is
watching: a test fixture tearing down, or a script cleaning up staging.

**Blanket `RESTRICT`, as principle 4 requires.** The stricter reading, and the one
the package's own principles endorse. Rejected because it buys nothing under
anonymisation — no `users` row is ever hard-deleted, so the cascades it forbids
would never have fired — while costing hand-written, individually-tested deletion
code on every 1:1 extension and junction table, for all 66 tables. It optimises
against a hazard the system's deletion model has already removed.

**Cascade everywhere plus an application-level guard.** Considered because it
keeps the DDL uniform. Rejected on the same grounds as every other control this
repository has had to fix: a guard in application code is absent from `psql`, from
a migration, and from a test fixture, which are the three places the accidental
`DELETE` will actually come from.

## Consequences

**`DELETE FROM users` now fails loudly** — with a foreign-key violation naming
`admin_users` or `user_legal_consents` — instead of succeeding and taking eight
tables with it. That is the whole practical value of this decision, and it is what
principle 4 was reaching for without saying so.

**A future hard-erasure job is forced to make an explicit decision about the two
restricted tables.** It cannot inherit one by writing `DELETE` and letting the
schema decide. If GDPR erasure ever has to go beyond anonymisation, "what happens
to the consent record" becomes a question somebody has to answer in code review
rather than one the database answers silently at 3am.

**Test fixtures must tear down in dependency order**, or delete the two restricted
children first. This is a real cost, paid on every future test that creates an
admin or a consent. It is the intended cost: the alternative is fixtures that
quietly exercise a destructive path production must never take.

**The package's contradiction is resolved for M1 only.** M2–M6 create dozens more
foreign keys, and the rule above is guidance, not a mechanism. Nothing forces a
later migration to apply it.

**This is the second recorded departure from the package's DDL**, after the ADR
0009 drops. ADR 0011 requires a departure to be stated where it happens; the M1
migration names this record.

### Confirmation

- **Mechanical:** an integration test asserts that deleting a `users` row removes
  its profile, onboarding, language and identity rows, and that the same delete is
  **rejected** when a consent row or an unrevoked admin grant exists. Both halves
  are needed — a test that only checks the cascades would pass against blanket
  cascade, which is the option being rejected.
- **Mechanical:** `test_models_and_the_chain_agree` fails if a model's `ondelete`
  and the migration's clause disagree, because both are visible to
  `compare_metadata`.
- **Not mechanical, and the real gap:** nothing checks that a *future* migration
  applies this rule. M2 can add a cascade to an audit table and every gate will
  pass. The only control is that this record exists and review reads it — the same
  class of weakness ADR 0011 names about the package and the chain drifting.
- **Not mechanical:** nothing verifies the anonymisation job — which does not
  exist yet — honours the retention this record assumes for the restricted tables.
  The schema makes destroying them hard; it does not make retaining them
  automatic.

### Open questions

- **Whether `ON DELETE SET NULL` is the better rule for `admin_users.granted_by`
  and `revoked_by`.** `RESTRICT` preserves the fact that a specific person granted
  the access; `SET NULL` would let a granting admin be erased while keeping the
  grant itself. The first is the stronger audit trail and the second is friendlier
  to erasure. Chosen `RESTRICT` because erasure is anonymisation here, which
  preserves the row and its id, so the tension is theoretical until a real
  hard-delete path exists. Revisit when one does.

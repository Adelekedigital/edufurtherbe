---
name: adr
description: Write and maintain Architecture Decision Records using the MADR format. Use when making a technical decision with long-term consequences, choosing between libraries/databases/patterns, when asked "why is it built this way", when superseding a prior decision, or when the build workflow flags that a change is a decision rather than an implementation.
---

# Architecture Decision Records

An ADR captures **one decision, its context, and its consequences** at the moment
it was made. It is a dated, immutable record — not living documentation.

Its purpose is to stop the same argument from being re-litigated every eighteen
months by people who cannot reconstruct why the current choice was made. The
most valuable content is not the decision; it is the **rejected alternatives and
why they lost**, because that is exactly what a newcomer proposes on week two.

## When to write one

Write an ADR when a choice is:

- **Costly to reverse** — a datastore, a framework, an auth model, a public API
  shape, a deployment topology
- **Non-obvious** — a reasonable engineer would pick differently
- **Cross-cutting** — it constrains code outside the module that introduced it
- **Contested** — the team disagreed, or you overruled an obvious default

The build workflow's Phase 0 asks this directly: *is this a decision or an
implementation?*

Do **not** write one for: naming, formatting, an obvious library choice with no
real alternative, or anything a code comment covers. ADR inflation is real — 60
records nobody reads is the same as zero records, but with more maintenance.

Rule of thumb: **if you would have to explain it in a design review, it is an ADR.**

## Format: MADR

One file per decision, `docs/adr/NNNN-kebab-case-title.md`, numbered sequentially
and never renumbered.

```markdown
# 0007. Use PostgreSQL row-level security for tenant isolation

- Status: Accepted
- Date: 2026-07-30
- Deciders: @alice, @bob
- Consulted: @security-team
- Informed: #eng-backend

## Context and Problem Statement

We serve multiple tenants from one database. Every query must be scoped to the
caller's tenant. We have already shipped two cross-tenant read bugs caused by a
missing `WHERE tenant_id = ...`, both found by customers rather than by tests.

How do we make tenant isolation a property of the system rather than a
convention every developer must remember?

## Decision Drivers

- Cross-tenant leakage is a Critical-severity class of bug
- ~40 existing repository methods would need auditing
- Team has moderate Postgres depth, no prior RLS experience
- Isolation must hold for ad-hoc queries and migrations, not just the ORM

## Considered Options

1. Application-level scoping enforced by code review
2. A repository base class that injects the tenant predicate
3. PostgreSQL row-level security with a per-request session variable
4. Database-per-tenant

## Decision Outcome

Chosen: **option 3, PostgreSQL row-level security**, because it enforces
isolation in the datastore itself, so it holds even for code paths that bypass
the repository layer — which is precisely where both prior incidents occurred.

### Consequences

Good:
- Isolation is enforced below the application; a forgotten `WHERE` cannot leak
- Applies to migrations, admin scripts, and psql sessions alike
- Auditable as a database-level policy rather than dispersed code

Bad:
- Every connection must set `app.current_tenant`; a missed setting fails closed
  (returns zero rows), which is safe but confusing to debug
- Connection pooling requires resetting the variable on checkout
- Team must learn RLS; adds Postgres-specific coupling
- Superuser connections bypass RLS — migration tooling needs a separate review

Neutral:
- Locks us to PostgreSQL. We have no active plan to move.

### Confirmation

An integration test suite asserts that a query under tenant A's session variable
returns zero rows from tenant B, for every table carrying `tenant_id`. CI fails
if a new table with `tenant_id` lacks a policy.

## Pros and Cons of the Options

### Option 1 — Application-level scoping via review
- Good: no new technology
- Bad: this is what we do today, and it has failed twice
- Bad: relies on human vigilance for a Critical-severity class

### Option 2 — Repository base class
- Good: cheap, idiomatic, catches the common path
- Bad: bypassable — raw SQL, migrations, and admin scripts skip it
- Bad: neither prior incident would have been prevented

### Option 4 — Database per tenant
- Good: strongest possible isolation
- Bad: migration fan-out across thousands of databases
- Bad: connection-pool exhaustion; operationally heavy for our tenant count

## More Information

- Prior incidents: INC-2026-014, INC-2026-031
- Postgres RLS docs: https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- Revisit if we adopt a second datastore engine.
```

## Status lifecycle

| Status | Meaning |
|---|---|
| `Proposed` | Under discussion; open PR |
| `Accepted` | Decided and in force |
| `Rejected` | Considered and declined — **keep the file**; it prevents re-proposal |
| `Deprecated` | No longer relevant; not replaced |
| `Superseded by 0012` | Replaced by a later ADR |

**Never edit or delete an accepted ADR to reflect a new decision.** Write a new
one, and update the old status line to `Superseded by NNNN`. An edited ADR
destroys the record of what was known at the time, which is the entire value.

Typos and broken links may be fixed. Reasoning may not.

## Writing well

- **Title states the decision, not the topic.** "Use PostgreSQL RLS for tenant
  isolation", not "Tenant isolation".
- **Context describes forces, not the solution.** If the reader can guess the
  outcome from the context section, the context is biased.
- **Give rejected options a real argument.** Strawmen are how a decision gets
  overturned six months later — the person re-proposing option 2 finds no
  genuine reason it lost.
- **Consequences must include the bad ones.** An ADR with only upsides is
  marketing. The negative consequences are what the next person needs.
- **Confirmation** — state how you would know the decision is being honoured.
  Without this, ADRs decay into aspiration.
- Aim for one page. If it needs more, the decision probably contains two
  decisions.

## Workflow

1. `docs/adr/` — take the next free number.
2. Copy `templates/madr.md`.
3. Open a PR with status `Proposed`. **The PR discussion is the design review.**
4. On approval, flip to `Accepted` and merge with `docs(adr): accept NNNN — title`.
5. Link the ADR from the code it constrains:

```python
# Tenant isolation is enforced by Postgres RLS, not by this predicate.
# See docs/adr/0007-use-postgresql-row-level-security.md
```

6. `docs/adr/README.md` holds the index; keep it current.

The template is in `templates/madr.md`.

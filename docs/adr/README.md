# Architecture Decision Records

Why this codebase looks the way it does. Read the relevant record before
proposing a change that contradicts it — and if the change is still right, the
answer is a new record that supersedes the old one, not an edit.

## The records

| # | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-bubble-export-strategy.md) | Bubble export strategy — snapshot-first, transport behind a port | Accepted |
| [0003](0003-read-only-freeze-cutover.md) | Cut over behind a read-only freeze | Accepted |
| [0004](0004-calendar-integration.md) | Own the OAuth client, write events, read availability on demand | Accepted |
| [0005](0005-data-platform.md) | Use Supabase for Postgres, auth and storage — as Postgres, not as a backend | Accepted |
| [0006](0006-messaging-build-vs-buy.md) | Build booking-scoped messaging; keep mentor–mentee conversation on-platform | Accepted |
| [0007](0007-adopt-the-migration-package-as-the-target-data-model.md) | Adopt the migration package as the target data model | Accepted |
| [0008](0008-institutions-hipolabs-registry.md) | Institutions — the hipolabs registry, populated on demand | Accepted — storage and autocomplete superseded by 0020 |
| [0009](0009-first-login-authentication.md) | First-login authentication — Supabase Auth, email code by default, link as a choice | Accepted — point 9 superseded by 0014 |
| 0010 | Message thread scope — booking-scoped vs standalone | Reserved by 0007 |
| [0011](0011-alembic-is-the-migration-chain.md) | Alembic is the migration chain; the package DDL is its specification | Accepted |
| [0012](0012-google-oauth-scopes-and-client-split.md) | Google OAuth — non-sensitive scopes, and one Cloud project per purpose | Proposed |
| [0013](0013-foreign-key-deletion-policy-for-user-owned-rows.md) | Foreign-key deletion policy for user-owned rows | Accepted |
| [0014](0014-our-own-user-id-with-supabase-auth-as-a-column.md) | `users.id` is ours; the Supabase auth id is a column | Accepted — provisioning and reconciliation answered by 0018 |
| [0015](0015-every-table-has-a-surrogate-primary-key.md) | Every table has a generated surrogate primary key | Accepted |
| [0016](0016-api-contract-foundations.md) | API contract foundations — Problem Details, cursor pagination, normalisation at the boundary | Accepted |
| [0017](0017-deployment-and-how-migrations-run.md) | Deployment — no Dockerfile, and migrations from a dispatched workflow | Accepted |
| [0018](0018-eager-auth-provisioning-and-how-it-recovers.md) | Auth provisioning is eager, idempotent, and silent | Accepted |
| [0019](0019-profile-images-move-to-supabase-storage.md) | Profile images move to Supabase Storage, keyed on their own content | Accepted |
| [0020](0020-mirror-the-institution-catalogue.md) | Mirror the institution catalogue, refreshed weekly | Accepted — supersedes part of 0008 |

## Conventions

**Format is Michael Nygard's**, as established by ADR 0001: title, `Date`,
`Status`, `Context`, `Decision`, `Consequences`. Records from 0004 onward add a
`Confirmation` subsection under `Consequences` — how you would know the decision
is being honoured, and explicitly which parts nothing checks. Stating a gate's
blind spots next to its coverage is a house rule, and a record with no blind
spots listed has usually not looked.

**Status starts at `Proposed` when the decision *blocks* work.** The pull request
discussion is the design review. On approval the status flips to `Accepted` and
merges as `docs(adr): accept NNNN — <title>`. ADRs 0008 and 0009 went this way:
each gated a migration phase that could not start until it was settled, so each
earned a review of its own.

**A record *implemented by* the same pull request lands `Accepted` directly.**
ADR 0011 shipped with the M0 migration chain it describes, and 0013 with the M1
schema that expresses it. Splitting these in two would review the rule apart from
the code enforcing it — reviewing it twice and testing it never. The two flows
were not distinguished here until 0013; before that the paragraph above described
0008 and 0009 as though they were the only shape.

**Records are immutable once accepted.** A decision that changes gets a new
record; the old one stays in place with its status set to `Superseded by NNNN`.
Where a record's premises turn out to be wrong, the correction goes in as a note
at the top and the original text stays — ADRs 0002 and 0003 both carry one. The
history of what was believed and when is the point.

**Where only part of a record is superseded, the status names the part.** ADR
0002 reads `Accepted — point 4 superseded by ADR 0007`, not `Superseded by 0007`,
because eight of its nine decisions still stand and blanket-marking it would
retire them silently. A reader who needs to know whether a record still applies
is badly served by a status that overstates in either direction.

**Numbers carry their provenance.** A figure that justifies a decision names its
source next to it. Row counts come from `docs/bubble-data-model.md`, which is
canonical; the public site's figures are marketing and understate the database.
This rule exists because it was broken once — see `failure-modes.md` in the
`project-conventions` skill.

## Numbering

Sequential, never reused, never renumbered. Take the next unused number.

## A note on tooling

The `/adr-new` command ships with the tier-1 standards package and refers to the
MADR template. **This project uses Nygard**, per ADR 0001. The discrepancy is
recorded in the `project-conventions` skill; the command is not edited, because
tier-1 assets are overwritten on update and the correction would not survive.

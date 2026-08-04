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
| 0008 | Institutions — ROR ID vs hipolabs registry | Reserved by 0007 |
| 0009 | First-login authentication — magic link vs OTP | Reserved by 0007 |
| 0010 | Message thread scope — booking-scoped vs standalone | Reserved by 0007 |
| [0011](0011-alembic-is-the-migration-chain.md) | Alembic is the migration chain; the package DDL is its specification | Proposed |

## Conventions

**Format is Michael Nygard's**, as established by ADR 0001: title, `Date`,
`Status`, `Context`, `Decision`, `Consequences`. Records from 0004 onward add a
`Confirmation` subsection under `Consequences` — how you would know the decision
is being honoured, and explicitly which parts nothing checks. Stating a gate's
blind spots next to its coverage is a house rule, and a record with no blind
spots listed has usually not looked.

**Status starts at `Proposed`.** The pull request discussion is the design
review. On approval the status flips to `Accepted` and merges as
`docs(adr): accept NNNN — <title>`.

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

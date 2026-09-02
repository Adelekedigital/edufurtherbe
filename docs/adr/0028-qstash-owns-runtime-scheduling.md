# 28. QStash owns application-runtime scheduling

Date: 2026-09-02

## Status

Accepted.

Supersedes ADR 0017 only on the current hosting provider, ADR 0020 only on the
institution-sync scheduling mechanism, and expands the QStash role established
by ADR 0025. Their other decisions remain in force.

## Context

The application is now hosted on Railway. FastAPI Cloud remains a possible
future host, not the current primary host. Five application jobs were timed by
GitHub Actions while delayed callbacks were delivered by QStash. That split
gave runtime operations two owners and made repository workflows part of the
production application control plane.

QStash already supplies signed HTTP delivery, retry semantics, recurring
schedules, and stable schedule identifiers without an always-on worker. GitHub
Actions remains well suited to repository and operator work.

## Decision

1. Railway is the current application host. The application entrypoint stays
   host-independent; FastAPI Cloud is only a future option.
2. Database migrations remain external and manually dispatched. Application
   startup never runs Alembic, preserving ADR 0017's safety boundary.
3. QStash owns recurring application jobs, delayed callbacks, event-driven
   asynchronous work, and retryable delivery.
4. GitHub Actions owns CI, security scans, releases, manual migrations, manual
   recovery, and manually dispatched QStash schedule reconciliation.
5. `config/runtime-schedules.json` is schedule policy. The Upstash dashboard is
   observed and deployed state. Overrides may change only cron and enabled state.
6. Production schedules ship disabled. Enabling them requires an explicit
   go-live reconciliation; merging code never changes external schedules.

Both signed HTTP delivery and recovery CLIs invoke `RuntimeJobs`, so there is
one business execution path. Every signed endpoint verifies the configured
public destination and raw body before parsing it.

## Consequences

Application cadence no longer depends on repository activity or GitHub's cron
queue. Operators must configure QStash credentials and `PUBLIC_BASE_URL` on
Railway. Schedule changes require a reviewed manifest change and a separate
reconciliation action.

### Confirmation

Tests validate strict manifest resolution, environment defaults, stable IDs,
zero-diff reconciliation, delivery-policy fingerprints, and signature rotation.
Database tests retain the at-least-once invariants. A repository search permits
application `schedule:` triggers only in engineering or operations workflows.

No automated check proves the Upstash dashboard matches the repository until a
reconciliation is run. The command prints the full policy and diff before any
mutation, and dry-run is the default.

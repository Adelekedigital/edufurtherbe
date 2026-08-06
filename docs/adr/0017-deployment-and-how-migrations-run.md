# 17. Deployment: a built Dockerfile, and migrations from a dispatched workflow

Date: 2026-08-06

## Status

Proposed

## Context

Settled decision #13 chose FastAPI Cloud with Railway as the named fallback, and
made the fallback conditional in its own words: *"the escape stays real only by
keeping a standalone Dockerfile exercised in CI and using no platform-native
queue, cron or secrets store — an untested exit is an intention, not a plan"*.

**There is no Dockerfile.** Only `docker-compose.yml`, which starts the local
Postgres the test suite uses. By decision #13's own standard the escape is
currently an intention.

The first real deploy attempt, on 2026-08-06, found two platform limits:

1. **No run command.** `fastapi run` discovers its target from six fixed paths,
   none of which match `src/app/main.py`. Solved by a root entrypoint shim, which
   is a workaround for a heuristic rather than a configuration.
2. **No release command.** There is no hook between build and start, so nothing
   can run `alembic upgrade head` as a deployment step.

The second one matters more, and it collides with two standing rules. Migrations
run as a separate step before new code rolls out and **never on application
startup** — startup migrations race across replicas, and with more than one
instance that race is concurrent DDL. And ADR 0003 requires the cutover to be
rehearsed end to end, on the same code path as the real run, with exactly one
attempt available.

Decision #13's reopen condition is *"the platform cannot carry the freeze
rehearsal, or a platform-native primitive becomes tempting"*. The first half has
fired. This record answers it without reopening the platform choice, because the
platform is not what the rehearsal actually needs.

## Decision

**Migrations do not belong to the serving platform. A Dockerfile exists and CI
builds it.**

1. **A `Dockerfile` is added, and CI builds it on every pull request.** Not to
   deploy from today — to make the exit real on the terms decision #13 already
   set. A Dockerfile nobody builds rots into the same intention that decision
   warned about, and discovers it has rotted on the day it is needed.

2. **Migrations run from a manually dispatched GitHub Actions workflow**, against
   a DSN held as an environment-scoped secret, using the same
   `uv run alembic upgrade head` the local gate and every test already exercise.
   Not from an operator's laptop, not from a platform hook this platform does not
   have.

   This is chosen over a platform release step even where one exists, because it
   is the only option that is **independent of who serves traffic**. Moving from
   FastAPI Cloud to Railway would not change how a migration is applied, which is
   precisely what makes the exit cheap.

3. **The freeze rehearsal dispatches the same workflow against a scratch
   database.** ADR 0003 asks for rehearsal on the same code path and forbids a
   "production mode" branch; the same workflow with a different secret is the
   strongest available reading of that. The rehearsal exercises the mechanism,
   not a description of it.

4. **The application never migrates on startup**, restated here rather than left
   implied. The absence of a release hook is exactly the pressure that makes a
   startup migration look reasonable, and it is the moment the rule exists for.

5. **FastAPI Cloud continues to serve traffic.** The entrypoint is solved, and
   nothing else about it has failed. Point 2 removes the only capability it was
   missing, so there is no longer a reason to move — and, having removed it, no
   longer much cost in moving later.

### Rejected alternatives

**Migrate on application startup.** Requires nothing new, and the platform's
shape pushes toward it. Rejected because it races across replicas and turns a
rolling deploy into concurrent DDL against one database. The strongest argument
for it is that with a single instance it works fine — which is true, and stops
being true silently, the first time the platform scales to two.

**Migrate from an operator's machine.** Zero setup, and it is what will happen by
default if this record does not exist. Rejected because it leaves no run history,
depends on one person's environment and one person's DSN, and during a read-only
freeze with a single attempt it puts the least repeatable step at the most
expensive moment. The strongest argument for it is that the first migration will
be supervised by someone watching closely — which is the argument for doing it
this way *once*, not for it being the mechanism.

**Move to Railway now**, for its native release command. It is the platform that
does natively what point 2 builds by hand. Rejected as premature: it costs a
platform migration during M1 to buy a capability a dispatched workflow already
provides, and the Dockerfile in point 1 makes the move cheap whenever it is
actually wanted. The strongest argument for it is that one mechanism is simpler
than two.

**No Dockerfile; accept the lock-in.** Cheapest, and honest only if decision #13
is rewritten to drop the fallback. Rejected because the fallback is load-bearing
for a cutover that cannot be retried, and an escape nobody has tested is the
thing decision #13 already named as not a plan.

## Consequences

**CI gains a container build.** Minutes per run and an image nobody deploys.
That is the price of the exit being real rather than stated, and it is the
cheapest form of the assurance decision #13 asked for.

**A production DSN becomes a GitHub environment secret.** Decision #13 says to
avoid a platform-native secrets store; this is a CI-native one, which is the same
category of dependency pointed at a different vendor. It is accepted knowingly:
the alternative is a credential on a laptop, and the migration step needs the
credential wherever it runs. Worth naming rather than pretending the constraint
was honoured in full.

**Applying a migration becomes a deliberate human act.** Someone dispatches the
workflow; nothing does it automatically on merge. That is the intent — a schema
change that lands without anybody choosing the moment is how a lock queue meets a
traffic peak — but it means a deploy can ship code whose migration nobody ran.

**The freeze runbook gains a step it can rehearse.** "Dispatch the migration
workflow against the production secret" is checkable in advance, unlike "someone
runs alembic".

### Confirmation

- **Mechanical, once built:** CI builds the Dockerfile on every pull request. A
  Dockerfile that stops building fails the build rather than being discovered on
  the day the exit is needed.
- **Mechanical, once built:** the migration workflow exists, and every
  application leaves a run record with an actor and a timestamp.
- **Not mechanical, and the largest gap:** nothing asserts that the deployed
  image and the migrated schema are the same revision. A deploy can ship code
  expecting a migration nobody dispatched, and the failure surfaces as a runtime
  error on the first request that touches the new column — not at deploy time.
  Closing it needs a check comparing the head revision the image expects against
  what the database holds, and this record does not build one.
- **Not mechanical:** nothing prevents a future contributor adding a startup
  migration. Point 4 is enforced by review against this record.
- **Not mechanical:** building the image in CI proves it builds, not that it
  runs. A Dockerfile that compiles and then exits on a missing environment
  variable would pass this check.

### Open questions

- **Can FastAPI Cloud deploy from a Dockerfile?** If it can, points 1 and 5
  collapse into one mechanism and the entrypoint shim stops being needed at all.
  Not investigated; it would simplify this record materially.
- **Who may dispatch the migration workflow**, and does it require a second
  approver for the production environment? GitHub environments support required
  reviewers; whether the cutover wants that gate or would be slowed by it is a
  judgement nobody has made.
- **Where does the production DSN actually live**, given the tension named in
  Consequences.
- **Does the image need the migration chain at all?** If migrations only ever run
  from CI, `migrations/` need not ship in the runtime image, which is a smaller
  image and one less way to run them by accident.

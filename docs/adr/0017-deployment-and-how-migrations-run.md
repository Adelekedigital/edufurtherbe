# 17. Deployment: no Dockerfile, and migrations from a dispatched workflow

Date: 2026-08-06

## Status

Accepted — hosting-provider portion superseded by ADR 0028. External/manual
migrations, no startup migrations, and the host-independent entrypoint remain.

## Context

Settled decision #13 chose FastAPI Cloud with Railway as the named fallback, and
made the fallback conditional in its own words: *"the escape stays real only by
keeping a standalone Dockerfile exercised in CI and using no platform-native
queue, cron or secrets store — an untested exit is an intention, not a plan"*.

There is no Dockerfile. The first draft of this record proposed adding one. That
was wrong, and why it was wrong is the useful part.

**Neither platform consumes a Dockerfile.** FastAPI Cloud builds from source —
*"FastAPI Cloud will take your files and will install and build your
application… following and respecting Python standards and common conventions"* —
and supports no user-supplied image, buildpack or build command. Railway connects
directly to a git repository and takes a run command, which is how this project's
operator already runs Railway services, with no Dockerfile anywhere.

Decision #13 therefore named a proxy rather than the thing it wanted. What it
wanted was an exit that had been tested. What it named was a container no target
platform reads.

The first real deploy attempt, on 2026-08-06, found two further limits:

1. **The entrypoint is configurable, but not discovered.** `fastapi run` looks for
   six fixed paths and `src/app/main.py` is none of them. The answer is
   `[tool.fastapi] entrypoint` in `pyproject.toml`, which the platform documents
   for exactly this layout. A root shim was shipped first, before those docs were
   read, and has since been deleted.
2. **There is no release command.** Nothing runs between build and start, so the
   platform cannot run `alembic upgrade head`. Its migrations page offers
   architectural guidance — *"when you create a new database migration, it should
   still be compatible with whatever is the old code that is currently running"* —
   and no mechanism.

The second limit collides with two standing rules: migrations run as a separate
step and **never on application startup**, and ADR 0003 requires the cutover
rehearsed end to end, on the same code path, with one attempt available.

The startup-migration rule is not theoretical here. FastAPI Cloud rolls out
*"gradually replacing old instances with new ones"*, so old and new code run
against one database at once and a startup migration is concurrent DDL across
replicas.

Decision #13's reopen condition is *"the platform cannot carry the freeze
rehearsal, or a platform-native primitive becomes tempting"*. The first half has
fired.

## Decision

**Migrations do not belong to the serving platform, and the exit does not need a
container.**

1. **No Dockerfile.** The exit is a standard Python package plus a documented run
   command, and CI already exercises exactly that: `uv build` produces a wheel and
   an sdist on every pull request, and `UV_FROZEN=1` proves the lockfile resolves
   without drift. Railway consumes a git repository and a run command, and that
   command — `uvicorn app.main:app --app-dir src` — is already in the README and
   the Makefile.

   A Dockerfile would be a **second definition of the runtime** — base image,
   Python version, install steps — that no target platform reads and that drifts
   from `pyproject.toml` the first time either changes. It would make the exit
   look tested without testing it.

   **This supersedes decision #13 on the mechanism, not the intent.** The intent —
   an exit that is real rather than stated — is kept, and made more honest in
   Consequences.

2. **Migrations run from a manually dispatched GitHub Actions workflow**, against
   a DSN held as an environment-scoped secret, using the same
   `uv run alembic upgrade head` the local gate and every test already exercise.

   Chosen over a platform release step **even where one exists** — Railway has
   one — because it is the only option independent of who serves traffic. Moving
   platforms would not change how a migration is applied, which is what keeps the
   exit cheap.

3. **The freeze rehearsal dispatches the same workflow** against a scratch
   database. ADR 0003 asks for rehearsal on the same code path and forbids a
   "production mode" branch; the same workflow with a different secret is the
   strongest available reading. The rehearsal then exercises the mechanism rather
   than a description of it.

4. **The application never migrates on startup**, restated rather than left
   implied. The absence of a release hook is exactly the pressure that makes a
   startup migration look reasonable, and the gradual rollout is why it is not.

5. **FastAPI Cloud continues to serve traffic.** The entrypoint is configured and
   point 2 removes the only capability it was missing. Having removed it, there
   is also little cost in moving later.

### Rejected alternatives

**Add a Dockerfile anyway, for portability beyond Railway.** It would open
container-only targets — Fly, Cloud Run, ECS — without further work. Rejected as
speculative: neither platform under consideration reads one, and a runtime
definition nobody executes tends to be wrong by the time it is first needed. The
strongest argument for it is that on the day a container target *is* wanted,
writing one under time pressure is worse than having it. That day is not visible,
and the file can be written then against a real target rather than an imagined
one.

**Migrate on application startup.** Requires nothing new, and the platform's
shape pushes toward it. Rejected because the rollout is gradual by design, making
this concurrent DDL across replicas. The strongest argument for it is that with a
single instance it works — which is true, and stops being true silently.

**Migrate from an operator's machine.** Zero setup, and what will happen by
default if this record does not exist. Rejected because it leaves no run history,
depends on one person's environment and DSN, and puts the least repeatable step
at the most expensive moment of a one-attempt freeze. The strongest argument for
it is that the first run will be closely supervised — an argument for doing it
that way *once*, not for it being the mechanism.

**Move to Railway now**, for its native release command. It is the platform that
does natively what point 2 builds by hand. Rejected as premature: it costs a
platform migration during M1 to buy a capability a dispatched workflow already
provides, and point 1 means the move needs no preparation. The strongest argument
for it is that one mechanism is simpler than two.

## Consequences

**Nothing is added to CI.** The first draft would have added a container build;
this relies on the `uv build` step that already runs. The exit costs nothing to
keep and nothing to maintain.

**The exit is still unrehearsed, and that is now stated rather than proxied.**
Nobody has stood this application up on Railway. A Dockerfile would not have
changed that — it would have produced a green step proving an image builds, not
that a deployment works. The honest form of decision #13's requirement is one
rehearsal on Railway, not a file.

**A production DSN becomes a GitHub environment secret.** Decision #13 says to
avoid a platform-native secrets store; this is a CI-native one, the same category
of dependency pointed at a different vendor. Accepted knowingly: the alternative
is a credential on a laptop, and the migration step needs the credential wherever
it runs.

**Applying a migration becomes a deliberate human act.** Someone dispatches the
workflow; nothing happens on merge. That is the intent — a schema change landing
without anybody choosing the moment is how a lock queue meets a traffic peak —
but it means a deploy can ship code whose migration nobody ran.

**The freeze runbook gains a step it can rehearse.** "Dispatch the migration
workflow against the production secret" is checkable in advance, unlike "someone
runs alembic".

### Confirmation

- **Mechanical:** `uv build` runs on every pull request and `UV_FROZEN=1` is set,
  so a package that stops building — or a lockfile that drifts from
  `pyproject.toml` — fails CI. That is the portability the exit rests on.
- **Mechanical, once built:** the migration workflow exists, and every
  application leaves a run record carrying an actor and a timestamp.
- **Not mechanical, and the largest gap:** nothing asserts that the deployed code
  and the migrated schema are the same revision. A deploy can ship code expecting
  a migration nobody dispatched, and it surfaces as a runtime error on the first
  request touching the new column rather than at deploy time. Closing it needs a
  check comparing the head revision the code expects against what the database
  holds, and this record does not build one.
- **Not mechanical:** the exit has never been exercised. CI proves the package
  builds; nothing proves Railway will run it.
- **Not mechanical:** nothing prevents a future contributor adding a startup
  migration. Point 4 is enforced by review against this record.

### Open questions

- **When is the Railway exit rehearsed once?** This is the real form of decision
  #13's requirement, and the only thing that converts the fallback from an
  intention into a plan. It needs an afternoon, not a file.
- **A second approver on `production` is deferred, not declined.** GitHub
  environments support required reviewers. With one operator the gate is friction
  today and protection during the freeze, so it is **a pre-cutover task**: set it
  before the rehearsal, not after the first mistake. Recorded here because a
  deferral nobody wrote down is indistinguishable from a decision not to.
- **Where the production DSN lives**, given the tension named in Consequences.
- **Does the deployed application need `migrations/` at all?** FastAPI Cloud
  builds from source, so the chain ships whether or not anything runs it. If
  migrations only ever run from CI, excluding them is a smaller artefact and one
  less way to apply them by accident.

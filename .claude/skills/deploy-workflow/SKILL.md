---
name: deploy-workflow
description: Branching, merging, and promotion discipline for backend services — shipping unfinished work inert behind config, the branch funnel and when bypassing it is legitimate, the promotion gate, migration ordering across environments, and rollback limits. Use when opening a PR, merging, promoting to another environment, deciding whether work is safe to merge unfinished, planning a hotfix, or asking whether a change can be rolled back.
---

# Deploy Workflow

Two ideas carry most of the value here:

1. **Unfinished work ships inert rather than staying unmerged.** Long-lived
   branches rot; merged-and-switched-off code stays tested against real
   infrastructure.
2. **A green pipeline is not a decision.** Automation covers what it covers;
   promotion is a judgment that someone makes deliberately.

## Merge-dark: shipping work that isn't ready

Un-ready features are **not** held out of the funnel. They ship **inert**, behind
a default-off config guard, and are activated by changing configuration — never
by a deploy.

- **The guard is first in the code path**, before any database read, external
  call, or side effect:

  ```python
  async def sync_contacts(...):
      if not settings.CONTACT_SYNC_ENABLED:
          return                      # off must be provably a no-op
  ```

- **Every gated feature ships a test proving off = no-op.** That test is what lets
  a reviewer approve a dark merge without reading the whole feature.
- **Activating config defaults to unset / `None` / `False`.** A feature that
  activates because someone forgot to set a variable is not gated.

Activation and reversal then become a one-line environment change — seconds, no
redeploy — which is far safer than gating go-live on a code deploy.

**Two things cannot ship dark** and need a deliberate, reviewed promotion
instead: a destructive or irreversible migration, and a change with no meaningful
"off" state, such as a column *type* conversion.

## The branch funnel

```
feature branch → integration branch → main/source-of-truth → production
```

Every change goes through a pull request. Merging to the integration branch
deploys to the development environment; promoting to production is a **separate,
deliberate step**, never an automatic side effect of a merge.

### When a side-door is legitimate

Branching straight from the source-of-truth branch and skipping integration is
defensible in exactly two cases:

- **Production-isolated hotfix.** An incident needs a fix now and routing it
  through integration would drag in unreleased work. Branch, fix, promote — **then
  sync the same fix back**, so the integration branch is never missing something
  production has.
- **Integration is blocked.** Its suite is red or it has diverged badly, and
  building on it would mean debugging someone else's problem first.

**State the cost so a side-door stays a decision rather than a habit:** every one
widens the divergence between environments. That divergence is what produces the
recurring "integration is behind" state and the migration-revision failures
below. A side-door buys isolation now and pays in drift later. Pay it back by
syncing.

## Migration ordering

Migrations run as a **separate step, before the new code rolls out** — never on
application startup, where replicas race each other. Schema leads code: a bad
migration blocks the deploy instead of shipping broken code.

- **One head.** `alembic heads` must return exactly one line. A migration open
  for a while re-parents its `down_revision` onto the current head before merge.
  Two heads breaks `upgrade head` outright.
- **Never apply an unmerged migration to a shared database.** Stamping a shared
  database at a revision the deploying branch does not contain leaves it pointing
  at a revision the code cannot locate — the deploy then fails with "can't locate
  revision" and **the environment is stuck**. Test migrations against a throwaway
  or branch database.
  Recovery: merge the branch that owns the revision, or downgrade from a checkout
  that *has* the intervening files. Prefer downgrade over a bare `stamp`, which
  leaves the schema ahead of the code.
- **Never run `downgrade` locally** if your migration environment reads the same
  connection string your development environment uses. Reversibility belongs in
  CI, against an ephemeral database.

## The promotion gate

Run before every promotion between environments:

```
[ ] Dry-run merge clean, and the merged tree equals the source tree
[ ] Migration applies from the TARGET's exact current revision — not just from empty
[ ] Exactly one migration head
[ ] No NEW mandatory setting the target environment lacks
    (a defaulted setting is fine; a half-set one that raises at boot is not)
[ ] Boot check — the app imports and starts
[ ] Full suite green
[ ] Merged tree contains BOTH changesets
[ ] Pre-merge SHA captured as a rollback anchor
```

The **both-changesets** box catches the three-way-merge trap: a branch cut from a
stale base can silently revert recent work, and the merge is clean because git has
no idea it mattered.

**What CI covers and what it does not.** CI runs at PR time, not at promotion
time — so migration-applies-from-*empty*, single-head, boot, and suite are
automated, while tree equality, migration-applies-from-the-*target's current
revision*, and both-changesets stay manual. Say this explicitly wherever the gate
is written down: a doc that claims a gate covers more than it does is worse than
no doc, because it retires a manual check that is still needed.

Do the merge in an **isolated worktree** whenever the main tree is dirty or
another session is active.

## Rollback

- Capture the target branch's pre-merge SHA **before** promoting. Rollback is
  then `git push --force-with-lease origin <anchor>:<branch>`.
- **A rollback of code is not always a rollback of data.** An additive migration
  is cleanly reversible. A lossy one — a numeric type conversion that rounds, a
  dropped column, a merged pair of fields — is **not** reversible once the new
  shape is in use.

**Know which kind you shipped before promising a rollback.** State it in the PR
body. For a lossy migration the honest plan is roll-forward with a fix plus a
verified backup, and saying so in advance is much cheaper than discovering it
during an incident.

## Parallel sessions

Before staging, run `git status` and stage **only your own files**. If a shared
generated artifact has picked up another session's change, stash their source,
regenerate, then restore. Never carry another session's half-done work into your
PR — it will be attributed to you and reviewed by nobody.

## Checklist

- [ ] Work that isn't ready ships inert, guard first, with an off = no-op test
- [ ] Config that activates a feature defaults to off
- [ ] On a feature branch; never committing the build directly on the trunk
- [ ] PR opened; CI green before merge — necessary, not sufficient
- [ ] Any side-door justified, its cost stated, and the fix synced back
- [ ] Exactly one migration head; migration applies from the target's revision
- [ ] Promotion gate run; pre-merge SHA captured
- [ ] Reversibility of every migration stated explicitly in the PR
- [ ] Only your own files staged

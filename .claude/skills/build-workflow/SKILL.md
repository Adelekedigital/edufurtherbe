---
name: build-workflow
description: The end-to-end delivery workflow for backend work — discuss, confidence check, approved checklist, implement, docs and commit, then verify. Use when starting any non-trivial unit of work, when the user asks to "build", "implement", "add", or "fix" something, when picking up a ticket, before declaring work done, or when unsure what the next step in the delivery lifecycle is.
---

# Build Workflow

Every build follows one sequence:

```
Discuss → Confidence Check → Checklist → Definition of Done → Implement → Docs + Commit
```

Then, before moving to the next unit of work, the **Build Verification Gate**.

**Never skip or reorder.** Jumping straight to implementation is the single most
common source of regressions, and it is not faster — the time reappears as rework
plus a defect. The Definition of Done is written *before* implementation so you
build to it and test against it, rather than rationalising afterwards that what
you built is what was wanted.

---

## Step 1 — Discuss

Before any code, talk the change through.

- **State the problem.** What exists, what is missing, what must change.
- **Surface the decisions.** If two valid approaches exist, name both and the
  trade-off. Do not silently pick.
- **Name the affected files** before opening any of them.
- **Define the boundary before the body.** For an endpoint that means the
  request/response models and status codes (`api-designer`); for a schema change,
  the migration phases (`db-migration`). The contract is the expensive part to
  get wrong.
- **Flag anything that conflicts with a settled decision** now, not after the
  code is written.

If the change needs an architectural shift, a new top-level package, or reverses
something previously decided, that is an **ADR**, not an implementation detail.
Use the `adr` skill.

---

## Step 2 — Confidence Check

Explicit, written, and not optional. Answer all five:

1. **How confident are we in this approach?**
2. **What bugs, errors, or regressions are expected?**
3. **What edge cases does the current structure not handle cleanly?**
4. **Is this the industry-standard approach?** *Name the reference* — Stripe for
   idempotency, S3 for storage semantics, RFC 7807 for error shapes. Industry
   standard is the default; deviating is allowed but requires a stated reason.
5. **Does this touch data one user must never see from another?** If yes, run the
   authorization checklist in `security-checker`, and — for multi-tenant systems —
   the `tenant-isolation` skill if the project has it. "It's scoped" is not an
   answer. Naming every read and write path is.

State the mitigation for each risk found.

**If confidence is not high, go back to Step 1.** Read more files, re-read the
schema, check the actual function signatures. Never begin implementation on low
confidence — the checklist will be wrong and every later step compounds it.

---

## Step 3 — Checklist

Write a numbered checklist of every discrete change. Present it and **wait for
explicit approval before touching any file.**

> **The checklist response contains no tool calls.** Text only. The plan must be
> fully readable and approvable before a single file is opened. A response that
> proposes a plan and simultaneously starts executing it has not been approved —
> it has been announced.

A good item is specific enough that "done" is unambiguous:

```
- [ ] Add `update` to the `from sqlalchemy import select` line
- [ ] Add `_parse_timestamp(value: str | None) -> datetime | None` after `_nonempty`
- [ ] Skip path: execute UPDATE before attachment, one commit for both
- [ ] Change the `"pending"` line in membership.py — NOT the adjacent `"active"` line
- [ ] Update the docstring to state that created_at is preserved from the source
```

That fourth item shows the pattern worth copying: when two similar lines sit
next to each other, **name the one that must not change**. Editing the adjacent
line is a real and recurring failure.

Do not add steps mid-implementation without flagging them.

---

## Step 4 — Definition of Done

Before any code, write the explicit, **testable** criteria that must all be true
for this build to count as done. Present it with or right after the checklist,
for approval. The Verification Gate checks against it.

Each criterion must be **observable**: a test asserts it, or a named manual check
confirms it. "Works well" is not a criterion.

Every DoD covers at minimum:

1. **Happy path** — the primary behaviour as a pass/fail assertion.
2. **Acceptance criteria** — from the ticket or PRD where one exists; otherwise
   derived from the decisions made in Step 1.
3. **Edge cases this build touches** — absent optional data, missing credential,
   retry, partial failure, empty result, ownership mismatch.
4. **Guardrails that must hold** — the invariants this change must not break.
5. **Bugs to watch for** — name the specific regressions this change could
   introduce, so the gate knows what to look for.

```
DoD — async batch status
[ ] POST /batches returns { batch_id } in < 300ms, does not block on the work
[ ] per-item status moves pending → running → ready/failed, readable via GET
[ ] a forced provider failure marks only that item failed; the batch completes
[ ] no vendor SDK imported outside the provider package (lint passes)
[ ] OpenAPI regenerated and matches the new response shape
```

**The DoD becomes tests wherever possible.** Prefer an automated assertion over a
manual check; reserve manual checks for what tests genuinely cannot reach. No
implementation begins until the DoD is written and approved.

---

## Step 5 — Implement

Work the checklist top to bottom.

### Read before writing

Before editing any file, read: the file itself, any model or service the change
depends on, and the signature of every function the new code calls. **Never write
from memory** — a signature recalled from earlier in the session may have changed
since, and a signature recalled from training was never real.

### Imports first

Add every new import before the code that uses it. Missing imports are the most
common first-run failure.

### Stay in scope

Implement exactly what the checklist says. Do not refactor surrounding code, add
error handling for impossible states, introduce abstractions for hypothetical
future needs, or add undiscussed features.

### Report per item, not in a batch

After each checklist item: what changed, what is next. Do not silently complete
six items and report at the end — that removes every opportunity to catch a wrong
turn early.

### Read back after every edit

Re-read the changed section **and the few lines either side**. The most common
regression is an edit that was correct in isolation and silently broke its
neighbour. Confirm indentation, logic, and that nothing adjacent moved.

---

## Step 6 — Docs + Commit

Code and docs ship together. A code change without its doc update is incomplete.

1. **Build the full discrepancy list before editing any doc.** For every section
   that makes a claim about the changed code, open the code and compare. Record
   every gap first, then fix them — piecemeal doc edits miss the ones nobody
   thought to look for.
2. **Grep for stale references** after updating: old field names, old counter
   names, old behaviour descriptions.
3. **One doc at a time**, finished completely, before starting the next.
4. **Cross-check docs against each other.** Shared facts — permission keys, value
   sets, enum members — must agree everywhere they appear. Duplicated facts drift;
   the duplicate you forget is the one that becomes wrong.
5. **Update the API contract in the same commit** as the route or schema change.
   Consumers build against it; a stale contract doc produces broken clients.

### Before staging

- **`.gitignore` is complete.** For every untracked path in `git status`, confirm
  it is either intentionally committable or ignored. Verify with
  `git check-ignore -v <path>`. Data exports and fixtures containing real records
  must be ignored.
- **No secrets in staged files.** Example env files carry placeholders only. Tests
  use dummy values. No real keys, connection strings, or personal data.
- **Review `git diff --stat`.** Every file in it should be intentional.

### Commit rules

- Code and docs in one commit — never split.
- First line is *what*; body is *why* and the key decisions.
- **Never commit without explicit approval.** Never push without explicit
  instruction.
- Conventional Commits — the release tooling parses them, so a sloppy message
  produces a wrong version number.

---

## The Build Verification Gate

**Run before declaring work done and moving on. This is separate from the
pre-commit check.** Pre-commit gates the commit; this gates the transition. Work
that commits cleanly but carries a latent bug is a regression waiting for the
next build to surface it.

1. **Syntax-check every modified file.** A file that will not parse crashes the
   service at startup, and nothing else in the gate catches it.
2. **Grep for stale patterns introduced** — deprecated methods, framework calls
   that leaked into `domain/`, class names left over from a rename.
3. **Confirm call-site completeness.** For any changed signature, grep every
   caller and confirm all were updated.
4. **Confirm `git diff --stat` matches the plan** — no unexpected files, and no
   file that should have changed but didn't.
5. **Re-read every edit with its surrounding lines.**
6. **Run the full suite.** Use the parallel invocation if the project has one; a
   suite slow enough to skip is a suite that gets skipped.
7. **Verify every Definition-of-Done criterion** from Step 4 — each passes, or its
   named manual check is done. Explicitly hunt the "bugs to watch for".
8. **Verify the docs against the code.** Every doc claim about the changed code,
   re-checked. Tree audit, stale-reference grep, and parity of any list an agent
   relies on. No work is verified with stale docs.
9. **Re-run any data-isolation checks** if the build touched access-controlled
   data. A scoped list with an unscoped write is not a fix.
10. **State the verdict explicitly:** *"Verified — all DoD criteria met; no bugs,
    errors, omissions, or regressions found."* If something was found, fix it and
    re-run the gate.

The bar: a developer picking this codebase up cold should be able to install,
migrate, and boot it with no broken imports, no half-finished migrations, and no
silent deprecation bombs.

### Green CI is necessary, not sufficient

Two things that look like verification and are not:

- **A green job is not evidence a step ran.** A step marked `continue-on-error`,
  a tool that failed to install, a check that scanned zero files — all report
  green. Make checks blocking, pin tool versions, and make a check that finds
  nothing to scan **fail** rather than pass.
- **State a gate's blind spots next to its coverage.** A doc claiming a gate
  covers more than it does is the worst kind of stale doc: it retires a manual
  check that is still needed. When you automate half a check, say which half.

---

## The PR Review Procedure

Run before merging, **against the actual diff** — never from the PR description
or from memory. CI-green, mergeable PRs routinely carry real defects; this is
what catches them.

1. **Read the whole diff**, not the summary. Open each changed file at the change
   site.
2. **Check every claim against the code.** For each assertion in the PR body,
   commit message, or a doc it touches, verify it in the source. "Closes X" means
   finding the path that closes X — *and the paths that do not*.
3. **Hunt the partner-fix.** When a change widens or narrows a condition, find
   every other place that must move with it. A filter widened in one query and
   not its sibling is the classic half-fix.
4. **Trace consumers of anything whose shape or states changed.** Who reads this
   column, status, or field, and does any of them assume an invariant this
   change breaks?
5. **Docstrings that ship are contract changes.** Where a framework puts
   docstrings into the API spec, editing one changes the contract. Regenerate,
   and grep the spec for the old wording.
6. **Verify the docs this change makes stale — including ones it does not touch.**
   Grep the claim, not the filename.
7. **New pattern, new rule.** If the PR introduces a pattern the codebase has
   never used, the standards doc needs a rule, or the next person will re-decide
   it differently.
8. **Confirm the tests fail without the fix.** Temporarily revert the change and
   run them. A test that passes either way locks nothing in.
9. **Scope check.** `git diff --stat` matches the stated plan — no unrelated
   file, no other session's work.

---

## The Deep Check rule

**When asked to review, deep-check, debug, audit, or verify — report findings
only. Do not make changes.**

The output is a list: what was found, file and line, proposed fix. Then stop and
wait for explicit approval.

This applies to status checks too. **Never report from memory or from a task
list** — read the actual files and report what the code shows. A status report
assembled from what you remember doing is fiction with a high confidence score.

When fixes are approved, state the impact first: which files change, which callers
are affected, whether tests need updating, whether regressions are expected.

---

## The Idempotency rule

Anything that may be re-run must be idempotent — migrations, imports, seeds,
backfills, webhook handlers, retried jobs. Before writing one, answer:

- What happens on a **second run with the same input**?
- Does it duplicate, raise, or correctly no-op?
- Is each operation **individually atomic** — if it dies halfway, is the data
  consistent?

The reference shape: pre-load existing keys, use `ON CONFLICT` for inserts, check
for existing links before creating them, and commit per logical unit so a
mid-run failure leaves completed units intact.

---

## Quick reference

```
[ ] Problem and approach discussed; settled-decision conflicts surfaced
[ ] Confidence check written — industry standard named, risks mitigated
[ ] Data-isolation check if access-controlled data is touched
[ ] Checklist approved — TEXT ONLY, no tool calls in the plan response
[ ] Definition of Done written and approved — testable, with bugs-to-watch
[ ] All affected files read before writing
[ ] Imports added first
[ ] Scope matches the checklist exactly — no drive-by refactors
[ ] Status reported after each item
[ ] Every edit read back with its surrounding lines
[ ] Discrepancy list built before touching docs
[ ] Stale-reference grep run
[ ] API contract docs updated in the same commit
[ ] .gitignore verified; no secrets staged
[ ] git diff --stat reviewed — only your own files staged
[ ] Commit explains why; approval obtained
[ ] PR Review Procedure run against the actual diff

--- VERIFICATION GATE ---
[ ] Every modified file parses
[ ] Stale-pattern grep clean
[ ] All changed call sites updated
[ ] diff --stat matches the plan
[ ] Edits read back with surrounding context
[ ] Full suite green
[ ] Every DoD criterion verified; bugs-to-watch checked
[ ] Docs verified against the code — updated or explicitly flagged
[ ] Isolation checks re-run
[ ] Verdict stated explicitly
```

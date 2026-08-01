# Failure Modes — TEMPLATE

This project's incident log. Every row is something that actually went wrong
**here**, not a generic risk.

## Why this file earns its place

Generic standards encode what usually goes wrong. This file encodes what has gone
wrong in *this* codebase — a much better predictor of what will go wrong next,
and the only one of the two that cannot be downloaded.

Three properties make a row useful:

1. **How it surfaced** matters as much as what broke. A defect found by a user
   three weeks later is a different problem from one CI caught in ninety seconds,
   even when the code change is identical.
2. **The prevention must be mechanical where possible.** A rule that says "be
   careful" prevents nothing. A lint, a test, a constraint, or a function
   signature that makes the wrong call unwriteable does.
3. **Name the thing that lied.** Most expensive incidents involve something that
   *looked* correct — a green suite, a passing gate, a doc, a name. Record what
   gave false assurance; that is usually the reusable lesson.

## Adding a row

Add one whenever something is found the hard way — in review, in CI, in
production, or by a user. **Including near-misses.** A defect caught by luck
during review is the same defect; the only difference is where it stopped.

Write it the same day. The detail that makes a row useful is the first thing to
fade.

## The log

| Failure | How it surfaced | Prevention |
|---|---|---|
| *A concrete description — the actual symptom, not a category* | *Where it was caught, and how long it had been live* | *The mechanism that stops a recurrence, and whether it is automated or still manual* |

### Worked example of a good row

| Failure | How it surfaced | Prevention |
|---|---|---|
| A security scanner had not run for six PRs | Job was green the whole time — the step failed on every run, `continue-on-error` swallowed the exit code | Scanners made blocking, every tool version-pinned, and a check that scans zero files now **fails** instead of passing. **A green job is not evidence a step ran.** |

That row is worth writing because the reusable lesson is not about the scanner.
It is that *green* meant "nothing objected," not "the check ran" — and that
distinction applies to every gate in the project.

## Rows worth stealing from other projects

These recur across codebases. Delete any that cannot happen here; keep the ones
that can, and let them earn their place before something teaches you the same
lesson at full price.

| Failure | How it surfaced | Prevention |
|---|---|---|
| A migration hard-coded existing constraint names, so the chain would not run against an empty database — no new environment could be provisioned | Only when CI first applied migrations from scratch; invisible for months | Guard every rename with an existence check in **both** directions; run the chain from empty in CI |
| A test asserted the buggy behaviour, so the suite stayed green through the whole incident | A person re-read the shipped behaviour weeks later | A green suite is not evidence of correct behaviour when the test came from the same misunderstanding as the code. Check the *requirement* against the test, not only the code against the test |
| Only the list endpoint was scoped; the action endpoint reached other owners' rows by id | Found during review of an unrelated change | Scope every read **and write** path through one shared predicate. A hidden row is not a protected row |
| A permissions bug that was really an identifier mismatch — a session's external id compared against a column holding an internal one | Debugged for hours in the permissions code, where the defect was not | Translate external ids once at the boundary; use distinct types so the mismatch is a type error |
| An adjacent line was edited instead of the intended one during a rename | Caught by the read-back step, before the next file | Name the line that must **not** change in the checklist item |
| A doc claimed a gate covered more than it did, retiring a manual check that was still needed | A defect the "covered" check could never have caught | State a gate's blind spots next to its coverage, every time |

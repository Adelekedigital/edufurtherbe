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
| The `pre_commit_guard.py` hook allowed `git commit --no-verify` — the exact command it exists to block. `json.load` rejects a byte-order mark, and the handler caught the parse error and returned 0, so any encoding quirk on stdin disabled the guard entirely | Never fired in anger. Found on the day it was written, by feeding it payloads directly instead of trusting that it was installed. It reported success on every single input, including the ones it was supposed to refuse | The guard now decodes `utf-8-sig` and, when the payload will not parse, scans the raw text rather than waving the command through — a malformed envelope can no longer switch it off. **A control that fails open is indistinguishable from no control, and it reports green either way.** Fail-open is correct for the formatter hook and wrong for anything whose job is to say no |
| The `pytest-unit` pre-commit hook passed under `uv run pre-commit run --all-files` and failed in the real git hook with `ModuleNotFoundError: No module named 'app'` | Only when the first genuine `git commit` was attempted. Every prior verification had been run through `uv run`, which put the project venv on PATH and hid it | Local hooks now invoke `uv run` explicitly instead of relying on `language: system` resolving against the caller's PATH. **Verify a hook the way it will actually be invoked.** `pre-commit run --all-files` under a wrapper is a different execution environment from `.git/hooks/pre-commit`, and only one of them is the one that matters |
| `core/config.py` silently ignored misspelled `EDUFURTHER_` environment variables, leaving defaults in place. `extra="forbid"` was assumed to cover it and does not — pydantic-settings drops env vars matching no field before validation ever sees them | Caught by a test written to assert the behaviour, which failed on first run. Had the test been written after the code, it would likely have been written to match what the code did | An explicit `model_validator` scans `os.environ` for unrecognised prefixed variables and refuses to start. **Watching the test fail is what found this** — the assertion encoded the intent, and the intent turned out not to be implemented. Matters more once payment secrets are configured this way |
| `project-conventions` and two ADRs asserted "roughly 700 members, 15 mentors, 5,000 mentoring minutes" as fact, and used those figures to justify decisions — the argument against dual-run, the case for hand-checking every mentor record, the scope of the PII in the gitignore rules. They were marketing numbers scraped from the public site. The database holds 1,200 users, 44 mentors and 1,073 session bookings — the mentor count was out by roughly 3x | Near-miss. Surfaced only when the legacy data model was finally shared, weeks of documents later. Nothing had failed; the figures were simply being quoted forward, and would next have been quoted into the mapping document | Row counts now come from `docs/bubble-data-model.md`, named as canonical in "Where the truth lives", and tier 2 says explicitly not to quote the public site. **A number in a settled-decisions table gets cited, not re-derived** — so its provenance matters as much as its value. Where a figure justifies a decision, name the source next to it. Still manual: nothing checks that a documented number matches reality |
| Every job in the Security workflow was broken, and nobody could have known, because the workflow had never run. The secret scan 403'd on `/pulls/{n}/commits` for want of `pull-requests: read`; `pip-audit --strict` died on our own editable package, which will never be on PyPI; and CodeQL analysed successfully then failed at upload because GitHub Advanced Security is unavailable on a private personal repo | The first time the workflows ever executed — on the first pull request of the project, minutes after the repo gained a remote | Permissions fixed; the dependency audit now runs against the exported lockfile with `--no-emit-project`, so our own package is excluded by construction rather than suppressed; CodeQL removed outright. **A workflow that has never run is not a passing workflow, it is an untested one.** The config had looked correct for as long as the repo existed |
| The obvious fix for the `pip-audit` failure was dropping `--strict`, which would have gone green instantly | Noticed while fixing it, not after | `--strict` is what converts "could not audit this dependency" from a silent pass into a failure. Removing it fixes the symptom by disabling the check for all 180-odd real dependencies too. **When the quickest way to green is deleting a flag, the flag is usually the thing doing the work** — the same reasoning applies to `continue-on-error`, `|| true`, and lowering a coverage floor |
| CodeQL was removed rather than guarded with `if:` or `continue-on-error` | Decision made while fixing the above | A skipped job and a job that swallows its exit code both render as a green check. Leaving it in would have restored the exact failure this file's worked example describes, on the same day that example was read. **Prefer an absent check you have documented to a present check that cannot fail** |
| pre-commit pinned ruff 0.7.4 / mypy 1.13.0 / bandit 1.7.10 while the venv resolved 0.16.1 / 2.3.0 / 1.9.4 — a major-version mypy gap, so the local gate and CI would have disagreed about identical code | Near-miss. Noticed while checking installed versions for an unrelated reason, before it produced a confusing "passes locally, fails in CI" report | Those three are pinned with `==` in `[dependency-groups] dev` and kept equal to the `rev:` values in `.pre-commit-config.yaml`, with a comment at each site saying so. Ruff's `extend-exclude` was aligned with the hooks' `exclude` for the same reason. Still manual — `pre-commit autoupdate` does not touch `pyproject.toml` |
| The shared `settings` fixture read whatever `.env` sat in the working directory, so any field it did not pin explicitly silently took that file's value. Its docstring claimed the opposite | Found while writing a test for it — but the *first* version of that test passed against the unfixed code, because it probed `debug`, which the fixture pins as an init argument. Init arguments outrank a dotenv value, so the two fields the fixture set were exactly the two that could never show the leak | The fixture passes `_env_file=None`. The test probes `cors_origins` — a field the fixture leaves alone — and fails with the leaked value visible. **A test that passes the moment you write it, against a bug you believe is there, is a broken probe rather than good news.** Watching it fail is what distinguishes the two, and the first failure to look for is the test's own |
| Squash-merging the base of a stacked PR with `--delete-branch` closed the dependent PR permanently. GitHub will not reopen a PR whose head commits have since been rewritten by the follow-up rebase, will not accept a `--base` change on a closed PR, and recreating the deleted base branch unlocks neither | Immediately, on the first merge of the initial three-PR stack: PR #2 went from `OPEN` to `CLOSED` the moment PR #1 merged. Its content survived on the branch and shipped as a replacement PR; the original number and review history did not | Merge the parent **without** `--delete-branch`; rebase each child with `git rebase --onto main <old-base>`; force-push with lease; retarget with `gh pr edit --base main`; delete a branch only once no open PR targets it. The rebase requirement had been identified and written down *before* the merge, and the branch was deleted anyway. **Predicting a consequence does not prevent it — only a step in the procedure does.** A risk named in prose and absent from the command you actually run is not mitigated |

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

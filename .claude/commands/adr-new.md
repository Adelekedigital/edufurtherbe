---
description: Draft a new Architecture Decision Record from the MADR template
argument-hint: "<the decision being made>"
allowed-tools: Bash, Read, Write, Glob, Grep
---

Draft an ADR for: `$1`

Follow the `adr` skill.

## 1. Check it warrants an ADR

An ADR is warranted if the decision is costly to reverse, non-obvious,
cross-cutting, or contested. If it is none of those, say so and propose a code
comment or a line in `docs/architecture.md` instead. ADR inflation makes the
whole set worthless.

## 2. Find the next number and check for conflicts

```bash
ls docs/adr/
grep -rl "$(echo "$1" | tr ' ' '\n' | head -3 | tr '\n' '|')" docs/adr/ 2>/dev/null
```

Take the next unused sequential number. Never reuse or renumber. If an existing
accepted ADR covers or contradicts this decision, the new one **supersedes** it —
say so, and set the old record's status to `Superseded by NNNN`.

## 3. Gather the real context

Before drafting, establish:

- What forced this decision now? (an incident, a limit hit, a new requirement)
- What are the actual constraints — team skills, existing stack, timeline, cost?
- What are the genuine alternatives? At least two beyond the chosen one.
- What is the strongest argument **for** each rejected option? If you cannot
  make one, you have not understood it, and the decision will be re-proposed
  within a year by someone who has.

Ask the user for anything you cannot determine from the codebase. Do not invent
context — a fabricated rationale is worse than a missing ADR.

## 4. Write it

Copy the template at `skills/adr/templates/madr.md` to
`docs/adr/NNNN-kebab-case-title.md` and fill it in.

Requirements:

- Title states the **decision**, not the topic
- Context describes forces, not the solution — a reader should not be able to
  guess the outcome from it
- Rejected options get real arguments, not strawmen
- The **Bad** consequences section is not empty
- **Confirmation** states how you would know the decision is being honoured — a
  test, a CI check, a lint rule

## 5. Open it as Proposed

Status stays `Proposed`. Tell the user that the PR discussion *is* the design
review, and that on approval the status flips to `Accepted` and merges as
`docs(adr): accept NNNN — <title>`.

Update `docs/adr/README.md` with the new entry.

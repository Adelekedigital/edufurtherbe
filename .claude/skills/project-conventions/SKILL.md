---
name: project-conventions
description: This project's own settled decisions, house conventions, domain vocabulary, and guardrails — the things that are true here and nowhere else. Use at the start of any build in this repository, when choosing between two valid approaches, when a generic standard seems to conflict with how this codebase does it, before proposing a pattern the codebase has not used before, or when a requirement uses a term with a project-specific meaning.
---

# Project Conventions — TEMPLATE

> **Fill this in per project. Delete every line you have not made true.**
>
> This is tier 2 of the standards. The generic skills alongside it are
> project-agnostic and get overwritten on update; **this file is yours** and is
> never overwritten. Anything specific to this codebase belongs here, not in a
> generic skill.
>
> Keep it under roughly 300 lines. When a section outgrows that, move it to
> `references/` and link it — this file loads whenever the description matches,
> and length is a running cost.

## Settled decisions

Choices already made, recorded so they are not silently re-litigated. Each states
the decision, the reason, and what would have to change to reopen it.

| # | Decision | Why | Reopen if |
|---|---|---|---|
| 1 | *e.g. Authorization lives in application code; row-level security is off* | *Single-sourced and simpler while the API is the only client* | *Clients ever query the database directly* |
| 2 | | | |

Anything conflicting with a row here is an **ADR**, not an implementation detail.
Use the `adr` skill.

## Domain vocabulary

Words that mean something specific here. Ambiguity in this table is how scope
bugs get built.

| Term | Means precisely | Does **not** mean |
|---|---|---|
| *e.g. "account"* | *the billing entity* | *the login identity — that is a "user"* |
| | | |

> If a requirement uses one of these words in a way that could be read two ways,
> **ask**. Do not resolve it toward whichever reading is easier to build.

## House conventions

Local rules that override or extend the generic skills. Only what is actually
different here.

- **Naming:** *…*
- **Identifiers:** *which id space crosses which boundary*
- **Error envelope:** *the exact response shape*
- **Pagination:** *the default, and the bounds*
- **Vendor access:** *which package may import an SDK*

## Guardrails

Invariants every build must preserve, whatever it is doing. These become the
"guardrails" section of each Definition of Done.

- [ ] *e.g. no vendor SDK imported outside the provider package*
- [ ] *e.g. every outbound message checks consent first*
- [ ] *e.g. secrets never reach a log or a response body*

## Commands

```bash
# install, run, test, and the single command that runs the full local gate
```

## Where the truth lives

| Doc | Covers | Canonical copy |
|---|---|---|
| `CLAUDE.md` | Router; the few always-true facts | repo root |
| `docs/adr/` | Decisions and their rationale | repo root |
| | | |

**Canonical copy rule.** Where a doc is duplicated — a snapshot under a phase
folder, a copy in another repo — **name the one that wins** and edit only that.
Duplicated facts drift, and the copy you forget is the one that becomes wrong.

## Failure modes

`references/failure-modes.md` is this project's incident log: what broke, how it
surfaced, and what now prevents it. **Read it before any non-trivial build, and
add a row whenever something is found the hard way.**

That file is the most valuable thing in this overlay. Generic standards encode
what usually goes wrong; that file encodes what has actually gone wrong *here*,
which is a far better predictor of what will go wrong next.

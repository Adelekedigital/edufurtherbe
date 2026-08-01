# 1. Record architecture decisions

Date: 2026-08-01

## Status

Accepted

## Context

EduFurther is migrating from a Bubble low-code application to a Python backend
with a Next.js frontend, while simultaneously adding paid mentor sessions and
reshaping the data model rather than lifting it across unchanged.

That combination produces a lot of consequential, hard-to-reverse choices in a
short window — the Bubble export path, how identity migrates, the payment
provider, the cutover procedure, how money is represented. Decisions made in
chat are re-litigated later by whoever was not in the conversation, and the
reasoning behind a schema is the first thing to be lost.

## Decision

We record consequential decisions as Architecture Decision Records in
`docs/adr/`, numbered sequentially, in the format described by Michael Nygard.

A decision is consequential enough to need one when it is expensive to reverse,
constrains future work, or would surprise a competent engineer reading the code
without it. Anything that contradicts a row in the `project-conventions` skill is
an ADR, not an implementation detail.

Records are immutable once accepted. A decision that changes gets a new record
that supersedes the old one, and the old one stays in place with its status
updated — the history of what we believed and when is the point.

## Consequences

Decisions become reviewable in pull requests alongside the code they justify,
and a new contributor can read why the codebase looks as it does rather than
inferring it.

The cost is a short document per decision and the discipline to write it while
the reasoning is fresh rather than reconstructing it later.

Expected near-term records: Bubble export strategy, identity migration,
payment provider selection, money representation and the mentor payout ledger,
and the read-only freeze cutover procedure.

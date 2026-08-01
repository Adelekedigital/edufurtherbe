# 3. Cut over behind a read-only freeze

Date: 2026-08-01

## Status

Accepted

## Context

The Bubble application must be replaced by this backend and the new Next.js
frontend. There are three usual ways to move:

1. **Read-only freeze.** Stop writes to Bubble, migrate, point the new stack at
   the migrated data, resume. Brief scheduled downtime for writes.
2. **Dual-run with sync.** Both stacks live, data synchronised in one or both
   directions, users moved gradually. No downtime.
3. **Dual-write.** The application writes to both stores during the transition.

Dual-run and dual-write buy zero downtime at the cost of a synchronisation
mechanism that is itself the most defect-prone part of any migration — and here
the two schemas deliberately differ (ADR 0002), so a sync layer would need to
maintain a bidirectional transform between shapes that were never meant to
correspond.

Against that: the community is roughly 700 members and 15 mentors, sessions are
scheduled rather than continuous, and there is currently no revenue to interrupt
— paid sessions arrive after cutover. A scheduled write freeze announced in
advance costs this user base very little.

## Decision

**Cut over behind a read-only freeze. No dual-run, no dual-write, no
synchronisation layer.**

The consequence that governs everything else: **there is exactly one attempt.**
With no reverse sync, data created in the new stack after cutover cannot be
reconciled back into Bubble. So:

1. **The migration is rehearsed repeatedly against a scratch database from a real
   snapshot before the freeze.** Not tested — rehearsed, as the whole sequence,
   end to end, until two consecutive runs are clean. The reshape bugs surface
   here or they surface in production.

2. **The rehearsal is the same code path as the real run.** No "production mode"
   branch. A rehearsal that exercises different code proves nothing about the run
   that matters.

3. **The freeze order is: announce, stop writes in Bubble, take the final
   snapshot, migrate, reconcile, smoke-test, open the new stack.** The
   reconciliation report from ADR 0002 must pass before traffic is admitted, and
   it is a gate rather than a formality.

4. **Rollback is: leave Bubble frozen but intact until the new stack has run
   clean for an agreed period.** Bubble is not cancelled, deleted, or unfrozen on
   cutover day. It is the rollback plan, and it stays available until it is
   demonstrably not needed. Rolling back means unfreezing Bubble and discarding
   whatever the new stack recorded — which is why the confidence period is short
   and closely watched.

5. **Schema changes stay expand-only for the whole window.** Nothing is dropped
   or renamed destructively until after the confidence period closes.

## Consequences

No synchronisation layer is built, and the most defect-prone component of a
typical migration does not exist. The transform runs once, in one direction,
against a fixed snapshot.

Users experience a scheduled write outage. It must be announced, and it should be
placed when mentor sessions are least likely to be scheduled.

The one-shot property is the real cost, and it is paid in rehearsal time rather
than in engineering a sync. Skipping rehearsals converts that saving into an
incident, because the freeze is the first and only time the code runs for real.

Bubble's subscription must be kept current through the confidence period. It is
the rollback plan, and its file storage still holds the original assets should a
re-download be needed.

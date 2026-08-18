# 23. Per-offering scheduling windows replace general availability

Date: 2026-08-18

## Status

Accepted.

Diverges from `docs/edufurther-migration/` by **having no counterpart there**
rather than by contradicting one. Per-session-type scheduling is one of only
three things the UI needs that the canonical package does not specify, so ADR
0007's authority is silent here and this record is where the shape is decided.

## Context

A mentor declares general availability once, in `availability_rules`, and every
offering is bookable inside it. The UI adds per-offering scheduling: an offering
can carry its own weekly windows.

The screen's copy says the windows **restrict** availability, and that word is
the trap. Its own example is Wednesday 5–8pm and Thursday 9am–1pm — a deliberate
evening slot and a deliberate morning one. Intersected with a normal
working-hours availability those yield **zero** slots, and the mentor sees an
empty calendar with nothing on the screen to explain it.

## Decision

**Windows replace general availability for the offering that has them.**

| offering has windows | bookable when |
|---|---|
| yes | **its windows only** — general availability does not apply |
| no | general availability, exactly as before |

Three consequences follow, and each is written down because none is obvious from
the sentence above.

**`availability_exceptions` still subtract, always.** Windows replace
*availability*, not *unavailability*. A mentor who blocked a date blocked it for
every offering, so the slot query switched the source of `rules` and deliberately
did not switch `exceptions`. A reader implementing this from the headline alone
would switch both.

**A mentor with windows and no general availability is bookable.** Before this,
no rules meant no slots. That state is now reachable and is not a
misconfiguration — nobody should later "fix" it by requiring general availability
as a precondition.

**The offering's read model must say which mode is in effect.** Precedence that
is only true in the backend is precedence nobody can act on: a mentor who sets
windows on one offering and expects their general availability to still apply has
been surprised by us. That is an API obligation this record names and the next
release owes — the surface for *managing* windows does not exist yet, and the
read model gains the flag with it.

## Consequences

**The implementation is a swap of one source, not a second code path.**
`bookable()` already takes a list of weekly windows and does not care which table
produced them, so `session_type_scheduling_windows` mirrors `availability_rules`
column for column. That is deliberate: a parallel slot algorithm for windowed
offerings would be the same maths written twice, and the copy that drifts is the
one nobody is looking at.

**The exclusion constraint is scoped to the offering rather than the mentor**,
which is the one meaningful difference from `availability_rules_no_overlap`. Two
offerings may legitimately cover the same hours; two windows on *one* offering
covering the same hours is a duplicate the grid would count twice.

**The risk was never the table.** `slot_store` is built and tested, and every
existing slot test assumes `availability_rules` is the only source. Every
offering in existence has no windows, so the whole current population depends on
the fallback.

### Confirmation

| claim | what checks it |
|---|---|
| an offering with no windows is unchanged | `test_an_offering_with_no_windows_is_unchanged`; removing the fallback fails **14 tests**, including the entire existing slot suite |
| windows replace rather than intersect | morning availability and an evening window; intersecting yields zero |
| exceptions still subtract | a blocked day empties a windowed offering |
| windows are per offering | two offerings on one mentor, different hours, asserted separately |
| a switched-off window falls back | the query, the index and the constraint all read `is_active AND deleted_at IS NULL` |

**Blind spots.**

- **Nothing yet writes a window.** The management surface is the next release, so
  every window in these tests is seeded directly. The read path is what this
  record decides and what is tested.
- **Nothing exposes which mode an offering is in.** Named above as an obligation;
  until it ships, a client cannot tell a windowed offering from one using general
  availability except by inference from the slots.
- **No test asserts the slot *instants*, only the counts.** The grid maths is
  already covered by the existing slot suite, and asserting offsets here would
  duplicate it while making these tests fail on a timezone change that is not
  their subject.

## Alternatives considered

**Intersect, as the screen's copy says.** Rejected on the mock's own example: it
yields zero slots for the exact configuration the feature is for.

**A `schedule_id` on the offering pointing at a shared `availability_schedules`
parent**, per settled decision #89. That is the right shape when a mentor wants
one *named* schedule reused across offerings, and it is more machinery than
per-offering windows need today. #89 stays open for when reuse is the
requirement; this table does not preclude it, because the read consults the
offering either way.

**A boolean "use general availability" toggle plus windows.** Two sources of
truth for one question, and they can disagree — a toggle set to *use general* on
an offering that has windows is a state with no defined answer. Emptiness already
carries the flag: no live windows means general availability.

# 22. A session type's classification and stage get vocabularies

Date: 2026-08-18

## Status

Accepted.

Diverges from `docs/edufurther-migration/`, which ADR 0007 makes canonical for
the target data model. `04_sessions.sql` specifies `session_types.category` and
`session_types.application_stage` as plain `text` with no constraint, and has no
`custom_stage_label` column at all. The package is not edited here (ADR 0007);
this record is the departure.

## Context

Both columns shipped as free `text`, with no constraint, no vocabulary and no
value in any row. They were deliberately withheld from the public contract, and
the reason was recorded at the time: publishing them *"would commit this contract
to a shape nobody has designed"*.

That was the right call while nothing had designed one. The fourteen UI screens
have now designed both, so the argument has **lapsed** rather than been
overruled — which matters, because the reasoning it rests on is still correct and
should keep governing the next undesigned column.

Free text was also doing active harm on one of them. `category` is the mentor's
answer to *what kind of help is this*, which is the same question
`service_offerings` already answers for mentee needs and mentor offers — and that
taxonomy exists precisely because free text turns "SOP help", "Statement of
Purpose" and "sop" into three values that match nothing (settled decision #53).
A free-text `category` on the offering is that failure reintroduced one table
along, on the axis matching actually joins on.

## Decision

**`category` becomes `service_offering_id`** — a nullable foreign key to the
existing six-row taxonomy. No new vocabulary.

The rename is not cosmetic. `service_offerings` **has its own `category`
column**, a display grouping of its rows, so a foreign key named `category`
pointing at a table with a different `category` one join away is a trap for the
next reader. `mentor_service_offerings` and `mentee_goal_needs` already call this
`service_offering_id`; this is the third.

Nullable because classifying an offering is optional: an unclassified one is
bookable and simply matches no filter. Requiring it would put a mandatory field
in front of the thing a mentor came to do.

**`application_stage` becomes a closed set** — `text` + `CHECK` per settled
decision #100, with an `ApplicationStage` `StrEnum` at the Pydantic boundary.
Five values: `early_exploration`, `drafting_stage`, `post_submission`,
`revisions`, `other`.

It is a closed set rather than a lookup table by the handoff's own test: adding a
value requires a code change anyway, because nothing can render a stage it does
not know about.

**`other` is kept deliberately, and it costs a column.** Its label lives in a new
nullable `custom_stage_label`, tied to it by a **symmetric** constraint:

```sql
CHECK ((application_stage = 'other') = (custom_stage_label IS NOT NULL))
```

**Both fields are published.** `SessionTypeRead` gains `service_offering`,
`application_stage` and `custom_stage_label`; the taxonomy is exposed as a
`code`/`display_name` pair rather than a bare slug, matching
`MentorProfileRead.offerings`.

## Consequences

**The symmetry is the part worth keeping.** The one-directional form —
`custom_stage_label IS NULL OR application_stage = 'other'` — is satisfied by a
null label, so it never requires the payload it exists to require. That is
exactly the shape `ck_mentor_profiles_custom_url_requires_custom_venue` had, and
it is why a mentor could sit on a `custom` venue with nowhere to meet. Verified
by rewriting the constraint into that form and watching precisely one test fail.

The rule is enforced **twice**: at the boundary so a mismatch is a `422` naming
the field, and in the database so it holds for the ETL and for `psql`. One shared
validator serves both write models, because the rule is one rule.

**One case the boundary deliberately does not judge.** On a `PATCH`, an absent
field means *leave it alone*, so a request naming only `custom_stage_label` has
nothing to compare against. Refusing it would refuse a legal edit — setting the
label on an offering that is already `other` — so it is allowed through and the
resulting row is still refused by the `CHECK`.

**`/me/session-types` breaks.** `category` was a free-text string and is now a
`service_offering` object. That endpoint is a management surface, so the blast
radius is one screen.

**`sessions.topic` is not converted**, though the same decision names it as the
same taxonomy from the mentee side. The ETL writes it from legacy on every
migrated session, so converting it is a data migration over real rows rather than
a schema change over an empty column. It belongs with the booking work.

**The backfill maps by slug and reports what it cannot.** The column is null on
every row today, but the release that added offering writes accepts `category` as
free text — so anything unmatched is nulled and named in the migration output
rather than disappearing.

### Confirmation

How you would know this is being honoured, and what nothing checks.

| claim | what checks it |
|---|---|
| the stage vocabulary matches the class | `test_every_converted_enum_has_a_check_naming_its_values` walks `TEXT_CHECK_ENUMS` against the live `CHECK` |
| the `CHECK` is symmetric | two tests, one per direction, written straight to the column so they bypass Pydantic |
| the boundary refuses the same pair | two parametrised API tests returning `422` |
| the taxonomy join is outer | `test_an_unclassified_offering_is_not_an_error`; making it inner fails 19 tests, because every offering is unclassified today |
| both lists carry the fields | one helper attaches the join to both selects, and a test asserts the pair on each |
| the public key set is exact | `test_the_response_carries_nothing_it_should_not` compares a set, so a fourth field fails whatever it is called |

**Blind spots, stated rather than implied.**

- **Nothing checks that the five stages are the *right* five.** They came from
  fourteen mock screens, not from data, and no row holds one yet. The first real
  usage is the first evidence, and `other` is the escape hatch that makes being
  wrong survivable rather than blocking.
- **Nothing prevents a `PATCH` that names only `custom_stage_label` from
  reaching the database with a mismatched pair.** That is deliberate — see the
  consequence above — and the `CHECK` is what catches it. The `500` a client
  receives in that case is worse than a `422` and better than the legal edit
  being refused.
- **Nothing compares `ApplicationStage` to the UI.** If a screen adds a sixth
  chip, the mismatch surfaces as a `422` in someone's browser rather than as a
  failing test here.

## Alternatives considered

**Keep `category` as free text and constrain it later.** Cheapest, and it
reintroduces the matching failure #53 exists to prevent, on the axis matching
joins on. A mentor's private grouping and the platform's matching taxonomy are
different concepts; if a private grouping is wanted later it is a different
column with a different name.

**A `session_type_categories` lookup table.** More machinery for the same six
rows the taxonomy already holds, and it would put two answers to *what kind of
help is this* in one schema — the exact conflation #53 was written to stop.

**Drop `other` and force a named stage.** Tidier, and it pushes anything the five
values do not cover into whichever is least wrong. The vocabulary was drawn from
mock screens rather than from data, so being incomplete is the expected case.

**Put the label on every stage.** Removes the constraint entirely and permits a
stale label to survive an edit — dead data that renders nowhere until something
starts reading it, which is how `custom_meeting_url` outlived its venue.

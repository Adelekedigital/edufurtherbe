"""Product rules for the intake form.

**One constant, and it lives here because it is a decision rather than a
mechanism.** `MAX_QUESTIONS` is what the product says a mentor may ask, not
something the database or the API layer knows — and the layer check is what
found that out: `api/routes/me_intake.py` importing it from `infra/` was an
`api` -> `infra` violation, which is the boundary doing its job rather than an
inconvenience to route around.

There is no column to hold this. "At most five rows in a group" is not
expressible as a `CHECK`, which sees one row, or as a unique index, which
enforces distinctness rather than cardinality. The only database mechanism is a
counting trigger — which is what #107 removed from this schema — so the rule is
enforced in the store and the count races. That cost is named at the call site.
"""

#: At most five questions on one offering's form.
#:
#: An intake form a mentee abandons is worse than no form. The number is the
#: product's answer to that and is not derived from anything, so changing it is
#: a one-line edit here rather than a migration — which is the whole reason a
#: product rule does not belong in a constraint.
MAX_QUESTIONS = 5

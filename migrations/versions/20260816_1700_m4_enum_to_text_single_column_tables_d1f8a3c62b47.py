"""Settled decision #100, step two: the seven single-column, single-table enums.

The first step that moves data, and chosen to be the dullest one that does. No
index predicate names any of these types, no function reads one, and the whole
set is 57 rows locally — so the shape below is proved somewhere a mistake is
cheap, and steps 3 to 8 copy it.

**The template, for the six that follow.** Per column, in this order:

1. ``ALTER COLUMN ... DROP DEFAULT`` — the default is ``'x'::some_enum`` and the
   type change cannot cast it. Skipped where there is no default.
2. ``ALTER COLUMN ... TYPE text USING column::text``
3. ``ALTER COLUMN ... SET DEFAULT 'x'`` — now a plain text literal
4. ``ADD CONSTRAINT ... CHECK (column IN (...))``

Then ``DROP TYPE`` once every column is off it. Steps 3, 6 and 8 add a fifth
concern this one does not have: dropping the indexes whose predicate names an
enum literal *before* step 2 and recreating them after, **with the same name**.
`pg_depend` is the authoritative list of those — an index appears against the
type only when its definition references it, which is exactly the drop-list.

**Written out rather than shared.** A helper emitting this DDL would be a live
symbol a migration depends on, and decision #43 keeps migrations self-contained
for the reason that replay is supposed to be stable: edit the helper in month
three and this revision stops doing what it did. The duplication is answered one
level up instead — `test_every_converted_enum_has_a_check_naming_its_values`
asserts the *outcome* for every converted column, whoever wrote the migration,
which a helper could never do for a hand-written one.

**No ``USING`` shortcuts.** ``column::text`` is written explicitly on every one:
PostgreSQL will not cast an enum to text implicitly in ``ALTER COLUMN ... TYPE``,
and a migration that relies on a cast it did not name is one PostgreSQL version
away from failing.
"""

from __future__ import annotations

from alembic import op

revision = "d1f8a3c62b47"
down_revision = "c9d4e2a71f68"
branch_labels = None
depends_on = None

#: ``(table, column, type name, labels in declaration order, default or None)``.
#:
#: Labels are transcribed from ``app.domain.enums`` and from the migrations that
#: created each type; no migration in this chain imports from ``app``, per
#: decision #43. **Declaration order is load-bearing in ``downgrade``** —
#: PostgreSQL sorts an enum by it, so recreating one alphabetised restores a type
#: that compares differently from the one dropped. The copies are pinned by
#: ``test_the_converted_enum_labels_have_one_meaning``.
#:
#: The constraint name is not stored: it is ``ck_{table}_{column}_is_known`` for
#: every row, and the test derives it that way so the convention cannot drift
#: silently in one of seven.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("users", "primary_role", "primary_role", "'mentee', 'mentor'", "mentee"),
    (
        "admin_users",
        "admin_role",
        "admin_role",
        "'super_admin', 'mentor_approval', 'limited_access'",
        None,
    ),
    ("auth_identities", "provider", "auth_provider", "'google', 'linkedin'", None),
    (
        "user_languages",
        "proficiency",
        "language_proficiency",
        "'native', 'fluent', 'conversational', 'basic'",
        "fluent",
    ),
    (
        "legal_documents",
        "type",
        "legal_document_type",
        "'terms_of_service', 'privacy_policy', 'mentor_agreement', 'community_guidelines'",
        None,
    ),
    (
        "user_awards",
        "verification_status",
        "verification_status",
        "'unverified', 'pending', 'verified', 'rejected'",
        "unverified",
    ),
    (
        "availability_exceptions",
        "type",
        "availability_exception_type",
        "'block', 'override'",
        None,
    ),
)


def upgrade() -> None:
    for table, column, type_name, labels, default in CONVERSIONS:
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'")
        # Bare name. `op.create_check_constraint` runs it through the same naming
        # convention that built every other CHECK here, so passing the rendered
        # `ck_users_primary_role_is_known` would produce
        # `ck_users_ck_users_primary_role_is_known`, truncated to 63 characters.
        op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")
        op.execute(f"DROP TYPE {type_name}")


def downgrade() -> None:
    """Restores the seven types and every column to them.

    Safe on data, unlike most contract-step downgrades: every value in these
    columns came out of the enum it is going back into, and the ``CHECK`` has
    guaranteed that for as long as it existed. The one thing a downgrade cannot
    restore is a value added while the column was text and absent from the enum —
    which is why it recreates the type first and lets the cast fail loudly rather
    than coercing.
    """
    for table, column, type_name, labels, default in reversed(CONVERSIONS):
        op.execute(f"CREATE TYPE {type_name} AS ENUM ({labels})")
        op.drop_constraint(f"{column}_is_known", table, type_="check")
        if default is not None:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {type_name} USING {column}::{type_name}"
        )
        if default is not None:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'::{type_name}"
            )

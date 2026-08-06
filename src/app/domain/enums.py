"""Closed vocabularies shared by the whole application.

**These live in ``domain/`` rather than beside the models, and that placement is
the point.** Each one is a fact about the product — what roles exist, which
providers can be linked — not a fact about PostgreSQL. Domain services will need
``PrimaryRole`` and cannot import ``infra/``, so a definition that starts in
``infra/db/models/`` would have to move the first time a business rule mentions
it. One definition, in the layer that owns the meaning, and ``infra`` imports it
to build the database type.

``StrEnum`` so a member compares equal to its wire value: ``PrimaryRole.MENTEE ==
"mentee"`` is true, which keeps JSON serialisation and string comparison honest
without a converter at every boundary.

**Values, not names, reach the database.** SQLAlchemy's ``Enum`` defaults to the
*member name* — ``MENTEE`` — which would create a PostgreSQL type whose labels are
uppercase and disagree with `docs/edufurther-migration/`. Every ``Enum`` column in
``infra`` therefore passes ``values_callable``; the helper for that lives beside
the models, since it is a SQLAlchemy concern.

The value lists are transcribed from `schema/00_foundation.sql` and must stay
identical to it. Adding a member here is a migration, not an edit — PostgreSQL
enum labels are schema.
"""

from enum import StrEnum


class PrimaryRole(StrEnum):
    """Which dashboard a user lands on. **Never an authorization check.**

    Authorization is profile existence: someone can be booked when an approved
    ``mentor_profiles`` row exists, and can book when a ``mentee_goals`` row does
    (package D2). A role column has to be kept consistent with those tables and
    can silently disagree with them; existence cannot, which is what makes dual
    roles free rather than a feature.

    The legacy data already disagrees with itself — the dev extract has a user
    whose ``Role`` is Mentee and who has a linked Mentor record — so this is not
    a hypothetical hazard being designed around.

    ``WHERE primary_role = 'mentor'`` in a permission check is a bug.
    """

    MENTEE = "mentee"
    MENTOR = "mentor"


class AdminRole(StrEnum):
    """Elevated access, held in ``admin_users`` rather than on the user row.

    The legacy design put an admin option set on ``User``, which made the grant
    un-revocable and left no record of who granted it.
    """

    SUPER_ADMIN = "super_admin"
    MENTOR_APPROVAL = "mentor_approval"
    LIMITED_ACCESS = "limited_access"


class AuthProvider(StrEnum):
    """External identity providers that can be linked to an account.

    Deliberately does not include email. An email login produces no
    ``auth_identities`` row — Supabase owns that path (ADR 0009), and the legacy
    ``Registration format`` value ``Email`` maps to the absence of a row rather
    than to a member here.
    """

    GOOGLE = "google"
    LINKEDIN = "linkedin"


class LanguageProficiency(StrEnum):
    """How well a user speaks a language.

    Ordered strongest to weakest as written, but **nothing depends on that
    order** — it is a set, and PostgreSQL enum ordering is label-declaration
    order, which is a trap worth not relying on.
    """

    NATIVE = "native"
    FLUENT = "fluent"
    CONVERSATIONAL = "conversational"
    BASIC = "basic"


class LegalDocumentType(StrEnum):
    """Which document a version of the terms represents.

    All four ship now even though only ``TERMS_OF_SERVICE`` has a consent to
    record at cutover. An enum member costs nothing until a row uses it, and
    ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction on older
    PostgreSQL — the asymmetry that makes enum members cheaper to declare than to
    add later.
    """

    TERMS_OF_SERVICE = "terms_of_service"
    PRIVACY_POLICY = "privacy_policy"
    MENTOR_AGREEMENT = "mentor_agreement"
    COMMUNITY_GUIDELINES = "community_guidelines"

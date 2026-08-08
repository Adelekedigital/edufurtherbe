"""Turning Bubble records into the rows this schema wants.

A package rather than a module because six phases of transforms are a documented
plan, not a hypothesis — the same reasoning `infra/db/models/` used, and the same
moment to act on it: splitting at two modules is nearly free, splitting at four
touches every import in the project.

**Everything ``identity.py`` exposed is re-exported here**, so the split changed
no call site. That is deliberate rather than lazy: a refactor that also rewrites
thirty imports is a refactor whose diff nobody reads carefully.

The division is by *subject*, matching settled decision #33 for models — M1's
identity Things in ``identity.py``, M2's profile Things in ``profiles.py``. What
they share is `domain/bubble.py`: one canonical record whichever source produced
it, so no transform here ever learns whether it came from the export or the API.
"""

from app.domain.transform.identity import (
    ADMIN_FIELD,
    ADMIN_ROLES,
    AUTH_PROVIDERS,
    AVATAR_FIELD,
    BANNER_FIELD,
    DEFAULT_TIMEZONE,
    NO_IDENTITY,
    ONBOARDING_COMPLETED_FIELD,
    PRIMARY_ROLES,
    PROFILE_LINK_FIELD,
    SLUG_PATTERN,
    AdminGrantRow,
    IdentityPlan,
    IdentityRow,
    OnboardingRow,
    ProfileRow,
    TransformError,
    TransformReport,
    UserRow,
    plan_identity,
    to_admin_grant,
    to_identities,
    to_onboarding,
    to_profile,
    to_user,
    transform_users,
)

__all__ = [
    "ADMIN_FIELD",
    "ADMIN_ROLES",
    "AUTH_PROVIDERS",
    "AVATAR_FIELD",
    "BANNER_FIELD",
    "DEFAULT_TIMEZONE",
    "NO_IDENTITY",
    "ONBOARDING_COMPLETED_FIELD",
    "PRIMARY_ROLES",
    "PROFILE_LINK_FIELD",
    "SLUG_PATTERN",
    "AdminGrantRow",
    "IdentityPlan",
    "IdentityRow",
    "OnboardingRow",
    "ProfileRow",
    "TransformError",
    "TransformReport",
    "UserRow",
    "plan_identity",
    "to_admin_grant",
    "to_identities",
    "to_onboarding",
    "to_profile",
    "to_user",
    "transform_users",
]

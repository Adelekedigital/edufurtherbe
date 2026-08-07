"""Every ORM model, re-exported here.

Importing this package is what registers models on ``Base.metadata``. Alembic's
``env.py`` imports it for exactly that reason: a model that is never imported is
invisible to autogenerate, which then proposes dropping the table it describes.

A package rather than a single ``models.py`` because 66 tables are a documented
plan, not a hypothesis — the later split would touch every import in the project.

**Add every new model to ``__all__`` below.** The test that inspects each mapped
class for its timestamp columns can only see models that have been imported, so
an omission here makes that test pass by looking at less.
"""

from app.infra.db.models.admin import AdminUser
from app.infra.db.models.education import DegreeLevel, Institution
from app.infra.db.models.legal import LegalDocument, UserLegalConsent
from app.infra.db.models.mentoring import ServiceOffering
from app.infra.db.models.reference import Country, Language
from app.infra.db.models.scholarships import ScholarshipProgram
from app.infra.db.models.user import (
    AuthIdentity,
    User,
    UserLanguage,
    UserOnboarding,
    UserProfile,
)

__all__ = [
    "AdminUser",
    "AuthIdentity",
    "Country",
    "DegreeLevel",
    "Institution",
    "Language",
    "LegalDocument",
    "ScholarshipProgram",
    "ServiceOffering",
    "User",
    "UserLanguage",
    "UserLegalConsent",
    "UserOnboarding",
    "UserProfile",
]

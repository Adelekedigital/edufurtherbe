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

from app.infra.db.models.admin import AdminUser, MentorAdminNote
from app.infra.db.models.availability import (
    AvailabilityException,
    AvailabilityRule,
    CalendarConnection,
    MentorAvailabilityChange,
    MentorNextAvailability,
    SessionTypeNextAvailability,
    SessionTypeSchedulingWindow,
)
from app.infra.db.models.credits import AdminCreditGrant, CreditLot, CreditTransaction
from app.infra.db.models.education import DegreeLevel, EducationEntry, Institution
from app.infra.db.models.intake import (
    IntakeAnswer,
    IntakeFile,
    IntakeFormQuestion,
    IntakeSubmission,
    SessionTypeQuestion,
    SessionTypeQuestionOption,
)
from app.infra.db.models.legal import LegalDocument, UserLegalConsent
from app.infra.db.models.mentoring import (
    FeaturedMentor,
    MenteeGoal,
    MenteeGoalCountry,
    MenteeGoalNeed,
    MenteeGoalSchool,
    MentorConferencingOption,
    MentorGuideCountry,
    MentorProfile,
    MentorServiceOffering,
    MentorStatusEvent,
    ServiceOffering,
    SessionTypeOffering,
)
from app.infra.db.models.missed_reasons import SessionMissedReason
from app.infra.db.models.platform import FeatureInterest, IdempotencyKey, OutboxEvent
from app.infra.db.models.reference import Country, Language
from app.infra.db.models.referrals import (
    InviteLink,
    InviteLinkRetiredCode,
    Referral,
    ReferralUnlock,
)
from app.infra.db.models.review_reports import ReviewReport
from app.infra.db.models.reviews import Review
from app.infra.db.models.scholarships import ScholarshipProgram, UserAward
from app.infra.db.models.sessions import (
    Session,
    SessionEvent,
    SessionParticipant,
    SessionRoomStay,
    SessionType,
    SessionTypeBookingConfig,
    SessionTypeStage,
)
from app.infra.db.models.suggestions import SessionSuggestion
from app.infra.db.models.user import (
    AuthIdentity,
    User,
    UserLanguage,
    UserLink,
    UserOnboarding,
    UserProfile,
)
from app.infra.db.models.writing_assist import WritingAssistRequest

__all__ = [
    "AdminCreditGrant",
    "AdminUser",
    "AuthIdentity",
    "AvailabilityException",
    "AvailabilityRule",
    "CalendarConnection",
    "Country",
    "CreditLot",
    "CreditTransaction",
    "DegreeLevel",
    "EducationEntry",
    "FeatureInterest",
    "FeaturedMentor",
    "IdempotencyKey",
    "Institution",
    "IntakeAnswer",
    "IntakeFile",
    "IntakeFormQuestion",
    "IntakeSubmission",
    "InviteLink",
    "InviteLinkRetiredCode",
    "Language",
    "LegalDocument",
    "MenteeGoal",
    "MenteeGoalCountry",
    "MenteeGoalNeed",
    "MenteeGoalSchool",
    "MentorAdminNote",
    "MentorAvailabilityChange",
    "MentorConferencingOption",
    "MentorGuideCountry",
    "MentorNextAvailability",
    "MentorProfile",
    "MentorServiceOffering",
    "MentorStatusEvent",
    "OutboxEvent",
    "Referral",
    "ReferralUnlock",
    "Review",
    "ReviewReport",
    "ScholarshipProgram",
    "ServiceOffering",
    "Session",
    "SessionEvent",
    "SessionMissedReason",
    "SessionParticipant",
    "SessionRoomStay",
    "SessionSuggestion",
    "SessionType",
    "SessionTypeBookingConfig",
    "SessionTypeNextAvailability",
    "SessionTypeOffering",
    "SessionTypeQuestion",
    "SessionTypeQuestionOption",
    "SessionTypeSchedulingWindow",
    "SessionTypeStage",
    "User",
    "UserAward",
    "UserLanguage",
    "UserLegalConsent",
    "UserLink",
    "UserOnboarding",
    "UserProfile",
    "WritingAssistRequest",
]

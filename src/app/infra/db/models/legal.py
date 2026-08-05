"""Versioned legal documents, and who accepted which version.

Legacy recorded a ``Terms agreed date`` and nothing else — *when*, never *what*.
With payments coming (tier 2 decisions #8 to #11) that is not enough: proving a user
accepted a particular version of the terms is a thing you either recorded at the
time or cannot reconstruct at all.

**Both tables ship empty.** The seed row representing the terms as they stand at
cutover needs a URL, a version string and an effective date, and the consent rows
need the ETL — both belong to M1c.

Worth knowing before that transform is written: in the dev extract,
``Terms agreed date`` equals ``email verified date`` on **all 43 rows**. They are
the same instant, stamped by one signup workflow, so the legacy column carries no
information independent of signup time. The migrated consents are honest — those
users did accept something at that moment — but they are not evidence of a
separate deliberate act, and nothing downstream should treat them as one.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import LegalDocumentType
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


class LegalDocument(TimestampMixin, Base):
    """One row per published version of one document type."""

    __tablename__ = "legal_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    type: Mapped[LegalDocumentType] = mapped_column(pg_enum(LegalDocumentType), nullable=False)

    # Free text rather than a parsed semantic version. What matters is that two
    # published documents are distinguishable and that a consent names one
    # exactly; ordering them is not a thing the database needs to do.
    version: Mapped[str] = mapped_column(Text, nullable=False)

    # NOT NULL, and that is load-bearing for M1c: a consent cannot be recorded
    # against a document nobody can produce. If there is no published terms page
    # to point at, that is a gap to close before cutover rather than a column to
    # relax.
    content_url: Mapped[str] = mapped_column(Text, nullable=False)

    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    __table_args__ = (Index("ix_legal_documents_type_version", "type", "version", unique=True),)


class UserLegalConsent(TimestampMixin, Base):
    """A specific user accepting a specific document version.

    **Both foreign keys are ``RESTRICT``** (ADR 0013). This is the row that
    answers "prove they agreed", and it is worthless if a cascade nobody was
    thinking about can destroy it. ``legal_document_id`` restricts for the
    mirror-image reason: deleting a document that people consented to would leave
    consents pointing at nothing, which is the same evidence loss by another
    route.
    """

    __tablename__ = "user_legal_consents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    legal_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("legal_documents.id", ondelete="RESTRICT"), nullable=False
    )
    consented_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    # Personal data under GDPR, kept because a consent record without the
    # originating address is weak evidence. It is also the column most likely to
    # be forgotten by a retention policy, so: this needs a purge job, and there
    # is not one yet.
    ip_address: Mapped[str | None] = mapped_column(INET)

    __table_args__ = (
        # One consent per user per document version. A second acceptance of the
        # same version is not a new fact.
        Index(
            "ix_user_legal_consents_user_document",
            "user_id",
            "legal_document_id",
            unique=True,
        ),
    )

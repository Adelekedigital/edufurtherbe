"""Mentoring: the shared vocabulary both sides of a match select from.

One module for mentor and mentee tables, because the thing that joins them —
``service_offerings`` — belongs to neither. Splitting by role would leave it
homeless. ``mentor_profiles``, the two junctions and the mentee goal tables join
this module in the next pull request; if it passes roughly 500 lines or seven
models, mentor and mentee separate and this table moves to its own module
(settled decision #33).
"""

import uuid

from sqlalchemy import Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class ServiceOffering(TimestampMixin, Base):
    """What a mentor helps with, and what a mentee needs — **the same six rows**.

    Both ``mentor_service_offerings`` and ``mentee_goal_needs`` point here, which
    is what turns "does this mentor do what this mentee needs" into a join
    (package D12).

    **D12's stated premise turned out to be wrong, and the correction matters
    more than the record does.** It says Bubble held two *separate* option sets
    with no mapping between them. It held **one**, used by both sides — but both
    columns store the display name as **text at the moment of selection** rather
    than a live reference, so they are snapshots taken at different times and at
    different depths of the same tree. The mentee side is six parent values; the
    mentor side is five parents plus five children and renames
    (``Statement of Purpose`` and ``Letter of Recommendation`` are children of
    Document Preparation, ``Visa Interview`` of Interview Preparation, and
    ``Document Review`` is an old name for Document Preparation). The decision
    D12 reached is right; its reason is not two vocabularies but **one recorded
    at inconsistent depth**.

    That is why the seed is the six parents, flat. All sixteen legacy values map
    onto them, and matching begins working immediately — today a mentor offering
    "Document Review" can never match a mentee needing "document preparation".

    **Closed by construction, and that is load-bearing.** No ``status``, no
    ``merged_into_id``, no ``usage_count``, no ``created_by``: users cannot add a
    row. This is the matching axis, and free text destroys it on contact — "SOP
    help", "Statement of Purpose" and "sop" become three rows matching nothing,
    while the join silently returns fewer results and nobody can tell why. The
    long tail is handled where it actually lives: ``institutions`` and
    ``scholarship_programs`` are both open, with the merge machinery to match.

    **Children are deliberately absent, and no ``parent_id`` is carried "for
    later".** ADR 0008 rejected exactly that shape — a column nothing populates
    is indistinguishable from a column somebody forgot to populate, and the first
    join written against it would be silently wrong on every row. Adding the
    hierarchy later is additive: a column plus rows, with existing junction rows
    still pointing at valid parents. What is *not* free is splitting or renaming
    a parent, since this table has no ``merged_into_id`` — so the six stay
    stable and all future specificity goes into children.
    """

    __tablename__ = "service_offerings"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # NOT NULL, unlike `scholarship_programs.slug`: every row here is authored by
    # the product, so every row has a stable identifier from the moment it
    # exists. It is what the ETL's mapping dict targets and what a test asserts
    # on — never `display_name`, which product is free to re-word, and never the
    # id, which differs in every environment.
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

    # application | test | funding | career. A display grouping, not a filter —
    # four of the six rows land in `application`, so it discriminates very
    # little. Seeded as the package specifies and worth nothing more than that.
    category: Mapped[str | None] = mapped_column(Text)

    sort_order: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

"""
AwardDecision model - represents the human decision to award an RFQ to a quotation.
Aligned with Procurement Bot Database Schema v2.0.

Key rules:
- Only ONE final award decision per RFQ (UNIQUE rfq_id).
- Selection is always a human decision (never automatic).
- reason can be made mandatory via reason_required flag.
"""
from sqlalchemy import (
    Column, String, Text, BigInteger, DateTime, Boolean,
    ForeignKey, UniqueConstraint, CheckConstraint, func
)
from app.database.models.base import Base, IdentityMixin


class AwardDecision(Base, IdentityMixin):
    """
    Represents the final award decision for an RFQ.
    """
    __tablename__ = "award_decisions"

    # The RFQ being awarded
    rfq_id = Column(
        BigInteger,
        ForeignKey("rfqs.id"),
        nullable=False,
        index=True,
        comment="RFQ ID being awarded"
    )

    # Company making the decision (RLS isolation scope)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
        comment="Company ID (RLS isolation scope)"
    )

    # The selected quotation
    quotation_id = Column(
        BigInteger,
        ForeignKey("quotations.id"),
        nullable=False,
        comment="Selected quotation ID"
    )

    # Who made the selection
    selected_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
        comment="User ID who made the selection"
    )

    # Reason for selection
    reason = Column(
        Text,
        nullable=True,
        comment="Reason for selection"
    )

    # Whether reason is mandatory
    reason_required = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether reason is mandatory"
    )

    # When the decision was made
    decided_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when the decision was made"
    )

    __table_args__ = (
        # Only one final award decision per RFQ
        UniqueConstraint(
            "rfq_id",
            name="uq_award_decisions_rfq"
        ),
        # Reason mandatory if reason_required is true
        CheckConstraint(
            "NOT reason_required OR btrim(coalesce(reason, '')) <> ''",
            name="ck_award_decisions_reason_required"
        ),
    )

    def __repr__(self):
        return (
            f"<AwardDecision(id={self.id}, rfq_id={self.rfq_id}, "
            f"quotation_id={self.quotation_id})>"
        )
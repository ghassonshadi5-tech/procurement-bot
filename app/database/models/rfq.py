"""
RFQ model - represents a Request For Quotation.
Aligned with Procurement Bot Database Schema v2.0.

An RFQ is created from an approved purchase request and sent to suppliers
(targeted, open, or mixed) with a deadline for submitting quotations.

Key rules:
- deadline must be after published_at (when both exist).
- published_at is mandatory once status leaves 'draft'.
- Closing the deadline does NOT auto-select a winner (human decision required).
"""
from sqlalchemy import (
    Column, BigInteger, DateTime, ForeignKey, Enum, CheckConstraint, Index
)
from app.database.models.base import Base, IdentityMixin, TimestampMixin
from app.database.models.enums import RfqMethod, RfqStatus


class Rfq(Base, IdentityMixin, TimestampMixin):
    """
    Represents a Request For Quotation (RFQ).
    """
    __tablename__ = "rfqs"

    # Company this RFQ belongs to (RLS isolation scope)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
        comment="Company ID (RLS isolation scope)"
    )

    # Source purchase request
    request_id = Column(
        BigInteger,
        ForeignKey("purchase_requests.id"),
        nullable=False,
        index=True,
        comment="Source purchase request ID"
    )

    # RFQ method: targeted, open, or mixed
    method = Column(
        Enum(RfqMethod, name="rfq_method"),
        nullable=False,
        comment="RFQ method: targeted, open, mixed"
    )

    # RFQ status
    status = Column(
        Enum(RfqStatus, name="rfq_status"),
        nullable=False,
        default=RfqStatus.draft,
        index=True,
        comment="RFQ status"
    )

    # Deadline for submitting quotations
    deadline = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Deadline for submitting quotations"
    )

    # When the RFQ was published
    published_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the RFQ was published"
    )

    # When the RFQ was closed
    closed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the RFQ was closed"
    )

    # User who created the RFQ
    created_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
        comment="User ID who created this RFQ"
    )

    __table_args__ = (
        # published_at is mandatory once status is not draft
        CheckConstraint(
            "status = 'draft' OR published_at IS NOT NULL",
            name="ck_rfqs_published_at_required"
        ),
        # deadline must be after published_at when both exist
        CheckConstraint(
            "deadline IS NULL OR published_at IS NULL OR deadline > published_at",
            name="ck_rfqs_deadline_after_published"
        ),
        Index("ix_rfqs_company_status", "company_id", "status"),
    )

    def __repr__(self):
        return (
            f"<Rfq(id={self.id}, company_id={self.company_id}, "
            f"method={self.method}, status={self.status})>"
        )
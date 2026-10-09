"""
PurchaseRequest model - represents a procurement request header.
Aligned with Procurement Bot Database Schema v2.0.

KEY ARCHITECTURAL CHANGES:
- Supports MULTIPLE items per request via purchase_request_lines
- Prices are NOT stored here (handled by pricing engine / quotations)
- 12-state lifecycle (draft → submitted → under_review → approved → rfq_created → ...)
- Approval/rejection uses unified decided_by / decided_at fields
- decision_note is MANDATORY for rejected and needs_revision statuses
- All changes are auto-logged via audit_trigger() in audit_log

CHANGE LOG:
- Fixed decision_note CHECK to properly reject NULL for rejected/needs_revision statuses.
- Removed duplicate index on requester_id (kept the named Index only).
"""
from sqlalchemy import (
    Column, String, Text, BigInteger, DateTime,
    ForeignKey, Enum, CheckConstraint, Index
)
from app.database.models.base import Base, IdentityMixin, TimestampMixin
from app.database.models.enums import RequestStatus


class PurchaseRequest(Base, IdentityMixin, TimestampMixin):
    """
    Represents the header of a purchase request.
    Line items are stored in purchase_request_lines.
    
    Status lifecycle:
    draft → submitted → under_review → approved → rfq_created → awarded → received → closed → archived
                                      ↘ needs_revision → submitted (resubmit)
                                      ↘ rejected (terminal)
    cancelled can happen from draft, submitted, or approved.
    """
    __tablename__ = "purchase_requests"

    # Company this request belongs to (RLS isolation)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
        comment="Company ID (RLS isolation scope)"
    )

    # User who created the request
    requester_id = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
        comment="User ID who created this request"
    )

    # Request title / summary
    title = Column(
        String(500),
        nullable=False,
        comment="Request title or summary"
    )

    # Request status
    status = Column(
        Enum(RequestStatus, name="request_status"),
        nullable=False,
        default=RequestStatus.draft,
        index=True,
        comment="Request status"
    )

    # When the request was submitted for review
    submitted_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when submitted for review"
    )

    # Who made the decision (approve/reject/needs_revision)
    decided_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who made the decision"
    )

    # When the decision was made
    decided_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the decision was made"
    )

    # Decision note (mandatory for rejected and needs_revision)
    decision_note = Column(
        Text,
        nullable=True,
        comment="Decision note (mandatory for rejected/needs_revision)"
    )

    __table_args__ = (
        # submitted_at must be NULL for draft, NOT NULL for all other statuses
        CheckConstraint(
            "(status = 'draft') = (submitted_at IS NULL)",
            name="ck_purchase_requests_submitted_consistency"
        ),
        # decided_by and decided_at must be set together or both NULL
        CheckConstraint(
            "(decided_by IS NULL) = (decided_at IS NULL)",
            name="ck_purchase_requests_decision_consistency"
        ),
        # decision_note is mandatory for rejected and needs_revision
        # Fixed: now properly rejects NULL
        CheckConstraint(
            "status NOT IN ('rejected', 'needs_revision') OR "
            "(decision_note IS NOT NULL AND btrim(decision_note) <> '')",
            name="ck_purchase_requests_decision_note_required"
        ),
        # Indexes for common queries
        Index("ix_purchase_requests_company_status", "company_id", "status"),
        Index("ix_purchase_requests_requester", "requester_id"),
    )

    def __repr__(self):
        return (
            f"<PurchaseRequest(id={self.id}, "
            f"company_id={self.company_id}, "
            f"title={self.title}, "
            f"status={self.status})>"
        )
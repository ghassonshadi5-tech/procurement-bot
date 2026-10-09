"""
Quotation model - represents a supplier's offer in response to an RFQ.
Aligned with Procurement Bot Database Schema v2.0.

Key rules:
- One active quotation per supplier per RFQ (UNIQUE rfq_id + supplier_id).
- submitted_at is mandatory once status leaves 'draft'.
- is_late flag marks quotations received after the deadline.
- guard_quotation_delete() trigger prevents deletion in terminal states.
"""
from sqlalchemy import (
    Column, String, Text, Integer, BigInteger, Numeric, Date, DateTime,
    Boolean, ForeignKey, Enum, UniqueConstraint, CheckConstraint, Index
)
from app.database.models.base import Base, IdentityMixin, TimestampMixin
from app.database.models.enums import QuotationStatus


class Quotation(Base, IdentityMixin, TimestampMixin):
    """
    Represents a supplier's quotation (offer) for an RFQ.
    """
    __tablename__ = "quotations"

    # Parent RFQ
    rfq_id = Column(
        BigInteger,
        ForeignKey("rfqs.id"),
        nullable=False,
        index=True,
        comment="Parent RFQ ID"
    )

    # Supplier submitting the quotation
    supplier_id = Column(
        BigInteger,
        ForeignKey("suppliers.id"),
        nullable=False,
        index=True,
        comment="Supplier ID"
    )

    # Company this quotation is addressed to (RLS isolation scope)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
        comment="Company ID (RLS isolation scope)"
    )

    # Quotation status
    status = Column(
        Enum(QuotationStatus, name="quotation_status"),
        nullable=False,
        default=QuotationStatus.draft,
        index=True,
        comment="Quotation status"
    )

    # Currency code (ISO 4217)
    currency = Column(
        String(3),
        nullable=False,
        comment="Currency code (ISO 4217)"
    )

    # When the quotation was submitted
    submitted_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when submitted"
    )

    # Whether the quotation was submitted after the deadline
    is_late = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="True if submitted after RFQ deadline"
    )

    # Delivery terms
    delivery_terms = Column(
        Text,
        nullable=True,
        comment="Delivery terms"
    )

    # Delivery cost
    delivery_cost = Column(
        Numeric(18, 4),
        nullable=False,
        default=0,
        comment="Delivery cost"
    )

    # Extra fees
    extra_fees = Column(
        Numeric(18, 4),
        nullable=False,
        default=0,
        comment="Extra fees"
    )

    # Lead time in days
    lead_time_days = Column(
        Integer,
        nullable=True,
        comment="Lead time in days"
    )

    # Quotation validity date
    valid_until = Column(
        Date,
        nullable=True,
        comment="Quotation validity date"
    )

    # Notes
    notes = Column(
        Text,
        nullable=True,
        comment="Notes"
    )

    # Who created / last updated
    created_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who created"
    )
    updated_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who last updated"
    )

    __table_args__ = (
        # One active quotation per supplier per RFQ
        UniqueConstraint(
            "rfq_id", "supplier_id",
            name="uq_quotations_rfq_supplier"
        ),
        # Currency format
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_quotations_currency_format"
        ),
        # submitted_at consistency
        CheckConstraint(
            "(status = 'draft') = (submitted_at IS NULL)",
            name="ck_quotations_submitted_consistency"
        ),
        # Non-negative costs
        CheckConstraint(
            "delivery_cost >= 0",
            name="ck_quotations_delivery_cost_non_negative"
        ),
        CheckConstraint(
            "extra_fees >= 0",
            name="ck_quotations_extra_fees_non_negative"
        ),
        CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0",
            name="ck_quotations_lead_time_non_negative"
        ),
        Index("ix_quotations_company_status", "company_id", "status"),
    )

    def __repr__(self):
        return (
            f"<Quotation(id={self.id}, rfq_id={self.rfq_id}, "
            f"supplier_id={self.supplier_id}, status={self.status})>"
        )
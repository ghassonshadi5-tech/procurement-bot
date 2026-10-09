"""
QuotationLine model - represents individual line items in a quotation.
Aligned with Procurement Bot Database Schema v2.0.

guard_frozen_quotation() trigger prevents modification/deletion
once the quotation reaches a terminal state.
"""
from sqlalchemy import (
    Column, String, Text, Integer, BigInteger, Numeric, Date,
    ForeignKey, UniqueConstraint, CheckConstraint
)
from app.database.models.base import Base, IdentityMixin


class QuotationLine(Base, IdentityMixin):
    """
    Represents a single line item in a quotation.
    """
    __tablename__ = "quotation_lines"

    # Parent quotation
    quotation_id = Column(
        BigInteger,
        ForeignKey("quotations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent quotation ID"
    )

    # Corresponding RFQ line
    rfq_line_id = Column(
        BigInteger,
        ForeignKey("rfq_lines.id"),
        nullable=False,
        comment="Corresponding RFQ line ID"
    )

    # Line number within the quotation
    line_no = Column(
        Integer,
        nullable=False,
        comment="Sequential line number"
    )

    # Unit price offered
    unit_price = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Unit price offered"
    )

    # Quantity available
    quantity_available = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Quantity available"
    )

    # Currency code
    currency = Column(
        String(3),
        nullable=False,
        comment="Currency code (ISO 4217)"
    )

    # Delivery date for this line
    delivery_date = Column(
        Date,
        nullable=True,
        comment="Delivery date"
    )

    # Line-specific terms
    line_terms = Column(
        Text,
        nullable=True,
        comment="Line-specific terms"
    )

    __table_args__ = (
        UniqueConstraint(
            "quotation_id", "line_no",
            name="uq_quotation_lines_quotation_line"
        ),
        CheckConstraint(
            "unit_price > 0",
            name="ck_quotation_lines_unit_price_positive"
        ),
        CheckConstraint(
            "quantity_available > 0",
            name="ck_quotation_lines_qty_positive"
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_quotation_lines_currency_format"
        ),
    )

    def __repr__(self):
        return (
            f"<QuotationLine(id={self.id}, quotation_id={self.quotation_id}, "
            f"line_no={self.line_no})>"
        )
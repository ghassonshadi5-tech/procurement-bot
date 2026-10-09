"""
GoodsReceipt model - represents the receipt of goods against an awarded quotation.
Aligned with Procurement Bot Database Schema v2.0.

Key rules:
- Only ONE receipt per quotation (UNIQUE quotation_id).
- Performed by the storekeeper (received_by).
"""
from sqlalchemy import (
    Column, String, Text, BigInteger, DateTime,
    ForeignKey, UniqueConstraint, func
)
from app.database.models.base import Base, IdentityMixin


class GoodsReceipt(Base, IdentityMixin):
    """
    Represents the receipt of goods for an awarded quotation.
    """
    __tablename__ = "goods_receipts"

    # Company (RLS isolation scope)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
        comment="Company ID (RLS isolation scope)"
    )

    # Source RFQ
    rfq_id = Column(
        BigInteger,
        ForeignKey("rfqs.id"),
        nullable=False,
        comment="Source RFQ ID"
    )

    # Awarded quotation
    quotation_id = Column(
        BigInteger,
        ForeignKey("quotations.id"),
        nullable=False,
        comment="Awarded quotation ID"
    )

    # Who received the goods (storekeeper)
    received_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
        comment="User ID who received the goods (storekeeper)"
    )

    # When received
    received_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when goods were received"
    )

    # Note
    note = Column(
        Text,
        nullable=True,
        comment="Note"
    )

    __table_args__ = (
        # Only one receipt per quotation
        UniqueConstraint(
            "quotation_id",
            name="uq_goods_receipts_quotation"
        ),
    )

    def __repr__(self):
        return (
            f"<GoodsReceipt(id={self.id}, quotation_id={self.quotation_id})>"
        )
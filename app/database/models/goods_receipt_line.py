"""
GoodsReceiptLine model - represents individual line items in a goods receipt,
including variance tracking between expected and received quantities.
Aligned with Procurement Bot Database Schema v2.0.

Key rules:
- variance_qty is a GENERATED column (quantity_received - quantity_expected).
- variance_reason is mandatory when there is a discrepancy.

CHANGE LOG:
- Fixed variance_reason CHECK to properly reject NULL when quantities differ.
- Added index=True on quotation_line_id.
- Added server_default="0" to quantity_received.
"""
from sqlalchemy import (
    Column, String, Text, BigInteger, Numeric,
    ForeignKey, CheckConstraint, Computed, text
)
from app.database.models.base import Base, IdentityMixin


class GoodsReceiptLine(Base, IdentityMixin):
    """
    Represents a single line item in a goods receipt.
    """
    __tablename__ = "goods_receipt_lines"

    # Parent goods receipt
    receipt_id = Column(
        BigInteger,
        ForeignKey("goods_receipts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent goods receipt ID"
    )

    # Corresponding quotation line
    quotation_line_id = Column(
        BigInteger,
        ForeignKey("quotation_lines.id"),
        nullable=False,
        index=True,
        comment="Corresponding quotation line ID"
    )

    # Expected quantity
    quantity_expected = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Expected quantity"
    )

    # Received quantity
    quantity_received = Column(
        Numeric(18, 4),
        nullable=False,
        server_default=text("0"),
        default=0,
        comment="Received quantity"
    )

    # Computed variance (received - expected)
    variance_qty = Column(
        Numeric(18, 4),
        Computed("quantity_received - quantity_expected"),
        comment="Computed variance (received - expected)"
    )

    # Reason for variance (mandatory when there is a discrepancy)
    variance_reason = Column(
        Text,
        nullable=True,
        comment="Reason for variance"
    )

    __table_args__ = (
        CheckConstraint(
            "quantity_expected > 0",
            name="ck_goods_receipt_lines_expected_positive"
        ),
        CheckConstraint(
            "quantity_received >= 0",
            name="ck_goods_receipt_lines_received_non_negative"
        ),
        # Variance reason mandatory when quantities differ
        # Fixed: now properly rejects NULL
        CheckConstraint(
            "quantity_received = quantity_expected OR "
            "(variance_reason IS NOT NULL AND btrim(variance_reason) <> '')",
            name="ck_goods_receipt_lines_variance_reason_required"
        ),
    )

    def __repr__(self):
        return (
            f"<GoodsReceiptLine(id={self.id}, receipt_id={self.receipt_id}, "
            f"expected={self.quantity_expected}, received={self.quantity_received})>"
        )
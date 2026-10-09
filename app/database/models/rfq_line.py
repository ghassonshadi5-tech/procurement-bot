"""
RfqLine model - represents individual line items in an RFQ.
Aligned with Procurement Bot Database Schema v2.0.

Each RFQ line references a purchase_request_line to maintain traceability
from the original request through the RFQ process.
"""
from sqlalchemy import (
    Column, Integer, BigInteger, Numeric,
    ForeignKey, UniqueConstraint, CheckConstraint
)
from app.database.models.base import Base, IdentityMixin


class RfqLine(Base, IdentityMixin):
    """
    Represents a single line item in an RFQ.
    """
    __tablename__ = "rfq_lines"

    # Parent RFQ
    rfq_id = Column(
        BigInteger,
        ForeignKey("rfqs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent RFQ ID"
    )

    # Source purchase request line (traceability)
    request_line_id = Column(
        BigInteger,
        ForeignKey("purchase_request_lines.id"),
        nullable=False,
        comment="Source purchase request line ID"
    )

    # Line number within the RFQ
    line_no = Column(
        Integer,
        nullable=False,
        comment="Sequential line number"
    )

    # Requested quantity
    quantity = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Requested quantity"
    )

    # Unit of measure
    uom_id = Column(
        BigInteger,
        ForeignKey("units_of_measure.id"),
        nullable=False,
        comment="Unit of measure ID"
    )

    __table_args__ = (
        # Line number must be unique per RFQ
        UniqueConstraint(
            "rfq_id", "line_no",
            name="uq_rfq_lines_rfq_line"
        ),
        # Quantity must be positive
        CheckConstraint(
            "quantity > 0",
            name="ck_rfq_lines_qty_positive"
        ),
    )

    def __repr__(self):
        return (
            f"<RfqLine(id={self.id}, rfq_id={self.rfq_id}, "
            f"line_no={self.line_no})>"
        )
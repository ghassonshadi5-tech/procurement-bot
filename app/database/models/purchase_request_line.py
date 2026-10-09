"""
PurchaseRequestLine model - represents individual line items in a purchase request.
Aligned with Procurement Bot Database Schema v2.0.

Each purchase request can have multiple lines.
A line can optionally reference a catalog item (item_id) if it exists,
or just have a free-text item_name for new/unknown items.
"""
from sqlalchemy import (
    Column, String, Text, Integer, BigInteger, Numeric,
    Date, ForeignKey, UniqueConstraint, CheckConstraint, Index
)
from app.database.models.base import Base, IdentityMixin


class PurchaseRequestLine(Base, IdentityMixin):
    """
    Represents a single line item in a purchase request.
    
    Attributes:
        request_id: Parent purchase request
        line_no: Sequential line number within the request
        item_name: Name of the item (always required, even if linked to catalog)
        item_id: Optional FK to catalog item (if matched)
        quantity: Requested quantity (must be > 0)
        uom_id: Unit of measure
        specs: Technical specifications / requirements
        delivery_date: Requested delivery date
        delivery_place: Delivery location
    """
    __tablename__ = "purchase_request_lines"

    # Parent purchase request
    request_id = Column(
        BigInteger,
        ForeignKey("purchase_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent purchase request ID"
    )

    # Line number within the request
    line_no = Column(
        Integer,
        nullable=False,
        comment="Sequential line number"
    )

    # Item name (always required)
    item_name = Column(
        String(500),
        nullable=False,
        comment="Item name"
    )

    # Optional catalog item reference
    item_id = Column(
        BigInteger,
        ForeignKey("items.id"),
        nullable=True,
        comment="Catalog item ID (if matched)"
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

    # Technical specifications
    specs = Column(
        Text,
        nullable=True,
        comment="Technical specifications / requirements"
    )

    # Requested delivery date
    delivery_date = Column(
        Date,
        nullable=True,
        comment="Requested delivery date"
    )

    # Delivery location
    delivery_place = Column(
        String(500),
        nullable=True,
        comment="Delivery location"
    )

    __table_args__ = (
        # Line number must be unique per request
        UniqueConstraint(
            "request_id", "line_no",
            name="uq_purchase_request_lines_request_line"
        ),
        # Quantity must be positive
        CheckConstraint(
            "quantity > 0",
            name="ck_purchase_request_lines_qty_positive"
        ),
    )

    def __repr__(self):
        return (
            f"<PurchaseRequestLine(id={self.id}, "
            f"request_id={self.request_id}, "
            f"line_no={self.line_no}, "
            f"item_name={self.item_name})>"
        )
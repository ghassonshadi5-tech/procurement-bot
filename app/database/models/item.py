"""
Item model - represents an item in a supplier's catalog.
Aligned with Procurement Bot Database Schema v2.0.

IMPORTANT ARCHITECTURAL CHANGE:
- Old: Items belonged to companies (tenant_id)
- New: Items belong to SUPPLIERS (supplier_id)

Items are the supplier's product catalog. Companies browse supplier
catalogs to create purchase requests and receive quotations.
"""
from sqlalchemy import Column, String, BigInteger, Boolean, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from app.database.models.base import Base, IdentityMixin, TimestampMixin


class Item(Base, IdentityMixin, TimestampMixin):
    """
    Represents an item in a supplier's catalog.

    Key changes from old model:
    - Belongs to a supplier (not a company)
    - Uses uom_id FK instead of free-text unit
    - Supports dynamic attributes via JSONB
    - Has SKU for inventory tracking
    - Supports category classification
    """
    __tablename__ = "items"

    # Supplier who owns this item
    supplier_id = Column(
        BigInteger,
        ForeignKey("suppliers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Supplier ID who owns this item"
    )

    # Category classification
    category_id = Column(
        BigInteger,
        ForeignKey("categories.id"),
        nullable=True,
        comment="Category ID"
    )

    # Stock Keeping Unit (unique per supplier)
    sku = Column(
        String(200),
        nullable=True,
        comment="Stock Keeping Unit"
    )

    # Item name
    name = Column(
        String(500),
        nullable=False,
        comment="Item name"
    )

    # Unit of measure (FK to units_of_measure)
    uom_id = Column(
        BigInteger,
        ForeignKey("units_of_measure.id"),
        nullable=False,
        comment="Unit of Measure ID"
    )

    # Dynamic attributes (JSONB)
    attributes = Column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Dynamic item attributes (color, size, material, etc.)"
    )

    # Whether the item is active in the catalog
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether the item is active in the catalog"
    )

    __table_args__ = (
        # SKU must be unique per supplier
        UniqueConstraint(
            "supplier_id", "sku",
            name="uq_items_supplier_sku"
        ),
        # GIN index for fast JSONB queries on attributes
        Index(
            "ix_items_attributes_gin",
            "attributes",
            postgresql_using="gin"
        ),
    )

    def __repr__(self):
        return f"<Item(id={self.id}, name={self.name}, supplier_id={self.supplier_id})>"
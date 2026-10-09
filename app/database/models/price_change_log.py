"""
PriceChangeLog model - tracks all price changes (Append-Only).
Aligned with Procurement Bot Database Schema v2.0.

This table is Append-Only: records cannot be updated or deleted.
The forbid_mutation() trigger in the database enforces this.

The log_price_change() trigger automatically populates this table
whenever price_rules or price_tiers are modified.
"""
from sqlalchemy import Column, BigInteger, ForeignKey, DateTime, Enum, func
from sqlalchemy.dialects.postgresql import JSONB
from app.database.models.base import Base, IdentityMixin
from app.database.models.enums import PriceType, AuditAction


class PriceChangeLog(Base, IdentityMixin):
    """
    Tracks all price changes (Append-Only).
    This table cannot be updated or deleted from.
    """
    __tablename__ = "price_change_log"

    # Price rule ID (no FK intentionally, to preserve history)
    price_rule_id = Column(
        BigInteger,
        nullable=False,
        comment="Price rule ID (no FK to preserve history)"
    )

    # Context references
    item_id = Column(
        BigInteger,
        nullable=True,
        comment="Item ID for context"
    )
    supplier_id = Column(
        BigInteger,
        nullable=True,
        comment="Supplier ID for context"
    )
    company_id = Column(
        BigInteger,
        nullable=True,
        comment="Company ID for context"
    )

    # Price type
    price_type = Column(
        Enum(PriceType, name="price_type"),
        nullable=False,
        comment="Price type: public or custom"
    )

    # Action
    action = Column(
        Enum(AuditAction, name="audit_action"),
        nullable=False,
        comment="Action type: INSERT, UPDATE, DELETE"
    )

    # Old and new data snapshots
    old_data = Column(
        JSONB,
        nullable=True,
        comment="Full snapshot of the record before change"
    )
    new_data = Column(
        JSONB,
        nullable=True,
        comment="Full snapshot of the record after change"
    )

    # Who made the change
    changed_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who made the change"
    )

    # When the change happened
    changed_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when the change happened"
    )

    def __repr__(self):
        return f"<PriceChangeLog(id={self.id}, price_rule_id={self.price_rule_id}, action={self.action})>"
"""
QuotationSnapshot model - immutable frozen snapshot of pricing data.
Aligned with Procurement Bot Database Schema v2.0.

CRITICAL: This table is Append-Only. The forbid_mutation() trigger
prevents any UPDATE or DELETE. This preserves the historical price
and terms even if the catalog or price rules change later.
"""
from sqlalchemy import (
    Column, String, Integer, BigInteger, Numeric, DateTime,
    ForeignKey, CheckConstraint, func, text
)
from sqlalchemy.dialects.postgresql import JSONB
from app.database.models.base import Base, IdentityMixin
from app.database.models.enums import PriceType


class QuotationSnapshot(Base, IdentityMixin):
    """
    Immutable snapshot of the pricing basis at the time a quotation
    is submitted or a comparison is approved.
    """
    __tablename__ = "quotation_snapshots"

    # Parent quotation
    quotation_id = Column(
        BigInteger,
        ForeignKey("quotations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent quotation ID"
    )

    # Corresponding quotation line
    quotation_line_id = Column(
        BigInteger,
        ForeignKey("quotation_lines.id"),
        nullable=False,
        comment="Corresponding quotation line ID"
    )

    # Price rule reference
    price_rule_id = Column(
        BigInteger,
        ForeignKey("price_rules.id"),
        nullable=True,
        comment="Price rule ID used"
    )

    # Price tier reference
    price_tier_id = Column(
        BigInteger,
        ForeignKey("price_tiers.id"),
        nullable=True,
        comment="Price tier ID used"
    )

    # Price type
    price_type = Column(
        String(20),
        nullable=False,
        comment="Price type: public, custom, tier, manual"
    )

    # Currency
    currency = Column(
        String(3),
        nullable=False,
        comment="Currency code"
    )

    # Frozen unit price
    unit_price = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Frozen unit price"
    )

    # Frozen quantity
    quantity = Column(
        Numeric(18, 4),
        nullable=True,
        comment="Frozen quantity"
    )

    # Frozen delivery cost
    delivery_cost = Column(
        Numeric(18, 4),
        nullable=False,
        default=0,
        comment="Frozen delivery cost"
    )

    # Frozen extra fees
    extra_fees = Column(
        Numeric(18, 4),
        nullable=False,
        default=0,
        comment="Frozen extra fees"
    )

    # Frozen lead time
    lead_time_days = Column(
        Integer,
        nullable=True,
        comment="Frozen lead time in days"
    )

    # Price basis description
    price_basis = Column(
        String(20),
        nullable=False,
        comment="Price basis: public, custom, tier, manual"
    )

    # Full frozen payload
    frozen_payload = Column(
        JSONB,
        nullable=False,
        comment="Complete snapshot of price and terms"
    )

    # When frozen
    frozen_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when frozen"
    )

    # Who froze it
    frozen_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who froze the snapshot"
    )

    __table_args__ = (
        CheckConstraint(
            "unit_price > 0",
            name="ck_quotation_snapshots_unit_price_positive"
        ),
        CheckConstraint(
            "delivery_cost >= 0",
            name="ck_quotation_snapshots_delivery_cost_non_negative"
        ),
        CheckConstraint(
            "extra_fees >= 0",
            name="ck_quotation_snapshots_extra_fees_non_negative"
        ),
        CheckConstraint(
            "price_basis IN ('public', 'custom', 'tier', 'manual')",
            name="ck_quotation_snapshots_price_basis"
        ),
        CheckConstraint(
            "jsonb_typeof(frozen_payload) = 'object'",
            name="ck_quotation_snapshots_payload_is_object"
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_quotation_snapshots_currency_format"
        ),
    )

    def __repr__(self):
        return (
            f"<QuotationSnapshot(id={self.id}, "
            f"quotation_id={self.quotation_id}, "
            f"price_basis={self.price_basis})>"
        )
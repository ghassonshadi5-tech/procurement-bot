"""
PriceTier model - represents quantity-based pricing tiers.
Aligned with Procurement Bot Database Schema v2.0.

Each price rule can have multiple tiers based on quantity ranges.
Example: Buy 1-10 units at $5, 11-50 units at $4.5, 51+ units at $4.

CRITICAL CONSTRAINT (enforced via Alembic migration):
EXCLUDE USING GIST prevents overlapping quantity ranges
for the same price rule.
"""
from sqlalchemy import Column, String, BigInteger, Numeric, ForeignKey, CheckConstraint, UniqueConstraint
from app.database.models.base import Base, IdentityMixin


class PriceTier(Base, IdentityMixin):
    """
    Represents a quantity-based pricing tier for a price rule.
    """
    __tablename__ = "price_tiers"

    # Price rule this tier belongs to
    price_rule_id = Column(
        BigInteger,
        ForeignKey("price_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Price rule ID"
    )

    # Minimum quantity
    min_qty = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Minimum quantity for this tier"
    )

    # Maximum quantity (NULL = no upper limit)
    max_qty = Column(
        Numeric(18, 4),
        nullable=True,
        comment="Maximum quantity for this tier (NULL = no upper limit)"
    )

    # Unit price for this tier
    unit_price = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Unit price for this tier"
    )

    # Currency code
    currency = Column(
        String(3),
        nullable=False,
        comment="Currency code (ISO 4217)"
    )

    __table_args__ = (
        # min_qty must be positive
        CheckConstraint(
            "min_qty > 0",
            name="ck_price_tiers_min_qty_positive"
        ),
        # unit_price must be positive
        CheckConstraint(
            "unit_price > 0",
            name="ck_price_tiers_unit_price_positive"
        ),
        # max_qty must be greater than min_qty if present
        CheckConstraint(
            "max_qty IS NULL OR max_qty > min_qty",
            name="ck_price_tiers_qty_range_valid"
        ),
        # Currency must be 3 uppercase letters
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_price_tiers_currency_format"
        ),
        # Unique min_qty per price rule
        UniqueConstraint(
            "price_rule_id", "min_qty",
            name="uq_price_tiers_rule_min_qty"
        ),
        # NOTE: EXCLUDE USING GIST constraint for preventing overlapping
        # quantity ranges should be added via Alembic migration:
        # EXCLUDE USING GIST (
        #     price_rule_id WITH =,
        #     numrange(min_qty, max_qty, '[]') WITH &&
        # )
    )

    def __repr__(self):
        return f"<PriceTier(id={self.id}, price_rule_id={self.price_rule_id}, min_qty={self.min_qty})>"
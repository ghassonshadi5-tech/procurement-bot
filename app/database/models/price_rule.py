"""
PriceRule model - represents pricing rules for items.
Aligned with Procurement Bot Database Schema v2.0.

Supports:
- Public prices (visible to all linked companies)
- Custom prices (visible only to a specific company)
- Time-bound validity (effective_from / effective_to)
- Approval workflow (status + approved_by)

CRITICAL CONSTRAINT (enforced via Alembic migration):
EXCLUDE USING GIST prevents overlapping active price rules
for the same (supplier, item, price_type, company).
"""
from sqlalchemy import Column, String, BigInteger, Numeric, DateTime, ForeignKey, Enum, CheckConstraint
from app.database.models.base import Base, IdentityMixin
from app.database.models.enums import PriceType, PriceStatus


class PriceRule(Base, IdentityMixin):
    """
    Represents a pricing rule for an item.
    A price rule can be public (for all companies) or custom (for a specific company).
    """
    __tablename__ = "price_rules"

    # Supplier who owns this price rule
    supplier_id = Column(
        BigInteger,
        ForeignKey("suppliers.id"),
        nullable=False,
        index=True,
        comment="Supplier ID"
    )

    # Item this price applies to
    item_id = Column(
        BigInteger,
        ForeignKey("items.id"),
        nullable=False,
        index=True,
        comment="Item ID"
    )

    # Price type (public or custom)
    price_type = Column(
        Enum(PriceType, name="price_type"),
        nullable=False,
        comment="Price type: public or custom"
    )

    # Company ID (NULL for public prices)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=True,
        index=True,
        comment="Company ID (NULL for public prices)"
    )

    # Currency code (ISO 4217)
    currency = Column(
        String(3),
        nullable=False,
        comment="Currency code (ISO 4217)"
    )

    # Base price
    base_price = Column(
        Numeric(18, 4),
        nullable=False,
        comment="Base price"
    )

    # Validity period
    effective_from = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="Start of validity period"
    )
    effective_to = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="End of validity period (NULL = open-ended)"
    )

    # Status
    status = Column(
        Enum(PriceStatus, name="price_status"),
        nullable=False,
        default=PriceStatus.draft,
        comment="Price rule status"
    )

    # Version
    version = Column(
        BigInteger,
        nullable=False,
        default=1,
        comment="Version number"
    )

    # Approved by
    approved_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who approved this price rule"
    )

    __table_args__ = (
        # Currency must be 3 uppercase letters
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_price_rules_currency_format"
        ),
        # Base price must be positive
        CheckConstraint(
            "base_price > 0",
            name="ck_price_rules_base_price_positive"
        ),
        # Public prices must not have company_id, custom must have it
        CheckConstraint(
            "(price_type = 'public' AND company_id IS NULL) OR "
            "(price_type = 'custom' AND company_id IS NOT NULL)",
            name="ck_price_rules_type_company_consistency"
        ),
        # Cannot be active without approval
        CheckConstraint(
            "status <> 'active' OR approved_by IS NOT NULL",
            name="ck_price_rules_approval_required"
        ),
        # Effective_to must be after effective_from
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_price_rules_validity_period"
        ),
        # NOTE: EXCLUDE USING GIST constraint for preventing overlapping
        # active price rules should be added via Alembic migration:
        # EXCLUDE USING GIST (
        #     supplier_id WITH =,
        #     item_id WITH =,
        #     price_type WITH =,
        #     COALESCE(company_id, 0) WITH =,
        #     tsrange(effective_from, effective_to, '[]') WITH &&
        # ) WHERE (status = 'active')
    )

    def __repr__(self):
        return f"<PriceRule(id={self.id}, item_id={self.item_id}, price_type={self.price_type})>"
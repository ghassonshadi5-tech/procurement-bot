"""
Supplier model - represents a supplier/vendor in the system.
Aligned with Procurement Bot Database Schema v2.0 (PostgreSQL 15+).

Suppliers are independent entities that can:
- Register on the platform
- Manage their own catalog and prices
- Receive purchase requests from multiple companies
- Respond with quotes

CHANGE LOG:
- Imported CompanyStatus from enums.py (Single Source of Truth) with re-export for backward compatibility.
- Added CHECK constraint for INN format (consistency with companies table).
- Clarified CHECK constraint for contact_email to handle NULL explicitly.
- Removed unused `import enum`.
"""
from sqlalchemy import Column, String, BigInteger, Boolean, DateTime, Enum, CheckConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from app.database.models.base import Base, IdentityMixin, TimestampMixin
from app.database.models.enums import CompanyStatus

# Re-export CompanyStatus for backward compatibility with existing imports
__all__ = ["Supplier", "CompanyStatus"]


class Supplier(Base, IdentityMixin, TimestampMixin):
    """
    Represents a supplier/vendor using the procurement bot.
    A supplier can work with multiple companies through supplier_relations.
    """
    __tablename__ = "suppliers"

    # Supplier name
    name = Column(
        String(500),
        nullable=False,
        comment="Supplier name"
    )

    # Tax ID (INN)
    inn = Column(
        String(12),
        nullable=True,
        comment="Tax ID (INN)"
    )

    # Business activity
    activity = Column(
        String(500),
        nullable=True,
        comment="Business activity"
    )

    # Cities where the supplier operates (array)
    cities = Column(
        ARRAY(String(200)),
        nullable=True,
        comment="Cities where the supplier operates"
    )

    # Contact email
    contact_email = Column(
        String(255),
        nullable=True,
        comment="Contact email address"
    )

    # Verification status
    is_verified = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether the supplier is verified"
    )

    # Timestamp when the supplier was verified
    verified_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the supplier was verified"
    )

    # Supplier status
    status = Column(
        Enum(CompanyStatus, name="company_status"),
        nullable=False,
        default=CompanyStatus.pending,
        comment="Supplier registration status"
    )

    __table_args__ = (
        # Ensure email format is valid (when present)
        CheckConstraint(
            "contact_email IS NULL OR contact_email ~ "
            "'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$'",
            name="ck_suppliers_email_format"
        ),
        # Ensure is_verified and verified_at are consistent
        CheckConstraint(
            "is_verified = (verified_at IS NOT NULL)",
            name="ck_suppliers_verification_consistency"
        ),
        # Ensure name is not empty
        CheckConstraint(
            "btrim(name) <> ''",
            name="ck_suppliers_name_not_empty"
        ),
        # INN format validation (10-12 digits when present)
        CheckConstraint(
            "inn IS NULL OR inn ~ '^[0-9]{10,12}$'",
            name="ck_suppliers_inn_format"
        ),
    )

    def __repr__(self):
        return f"<Supplier(id={self.id}, name={self.name}, status={self.status})>"
"""
Company model - represents a buyer company.
Aligned with Procurement Bot Database Schema v2.0.

Key changes in this update:
- Added phone_number, email for unique contact info.
- Added industry_code and industry_other_text for classification.
- Enforced uniqueness for phone and email at the application level (DB constraints added via migration/manual SQL).
- Imported CompanyStatus from enums.py (Single Source of Truth) with re-export for backward compatibility.
- Added CHECK constraints for approved_by/approved_at consistency and INN format.
- Added UNIQUE partial indexes for phone_number and email.
- Moved DateTime import to the top of the file.
"""
from sqlalchemy import (
    Column, String, BigInteger, ForeignKey, Enum,
    CheckConstraint, Index, DateTime, text
)
from app.database.models.base import Base, IdentityMixin, TimestampMixin
from app.database.models.enums import CompanyStatus

# Re-export CompanyStatus for backward compatibility with existing imports
# (e.g., `from app.database.models.company import Company, CompanyStatus`)
__all__ = ["Company", "CompanyStatus"]


class Company(Base, IdentityMixin, TimestampMixin):
    """
    Represents a buyer company on the platform.
    """
    __tablename__ = "companies"

    # Company Name (Unique check handled in service layer + DB constraint)
    name = Column(
        String(500),
        nullable=False,
        comment="Company trade name"
    )

    # Legal Name (Optional)
    legal_name = Column(
        String(500),
        nullable=True,
        comment="Official legal entity name"
    )

    # Tax ID / INN (Optional but recommended for verification)
    inn = Column(
        String(12),
        nullable=True,
        comment="Tax Identification Number (INN)"
    )

    # City
    city = Column(
        String(200),
        nullable=True,
        comment="Primary operating city"
    )

    # ✅ NEW: Unique Phone Number
    phone_number = Column(
        String(20),
        nullable=True,
        index=True,
        comment="Normalized company phone number (digits only)"
    )

    # ✅ NEW: Unique Email Address
    email = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Official company email address"
    )

    # ✅ NEW: Industry Classification Code
    # Values: manufacturing, construction, food_beverage, services, other
    industry_code = Column(
        String(50),
        nullable=True,
        comment="Predefined industry category code"
    )

    # ✅ NEW: Free Text Description if Industry is 'other'
    industry_other_text = Column(
        String(500),
        nullable=True,
        comment="Detailed description if industry_code is 'other'"
    )

    # Status of the company registration
    status = Column(
        Enum(CompanyStatus, name="company_status"),
        nullable=False,
        default=CompanyStatus.pending,
        comment="Current registration status"
    )

    # User who initiated the creation (for tracking)
    created_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="Internal User ID who created this request"
    )

    # Admin who approved/rejected
    approved_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="Super Admin internal ID who made the decision"
    )

    # Timestamp of approval/rejection
    approved_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the status was changed by admin"
    )

    __table_args__ = (
        # Ensure name is not empty
        CheckConstraint("btrim(name) <> ''", name="ck_companies_name_not_empty"),

        # ✅ NEW: Validate Industry Code against allowed list
        CheckConstraint(
            "industry_code IS NULL OR industry_code IN "
            "('manufacturing', 'construction', 'food_beverage', 'services', 'other')",
            name="ck_companies_industry_code_valid"
        ),

        # ✅ NEW: If industry is 'other', text description must be provided
        CheckConstraint(
            "(industry_code IS NULL) OR (industry_code != 'other') OR "
            "(industry_code = 'other' AND btrim(industry_other_text) <> '')",
            name="ck_companies_industry_other_consistency"
        ),

        # ✅ NEW: approved_by and approved_at must be consistent
        # (both NULL or both NOT NULL)
        CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL)",
            name="ck_companies_approved_consistency"
        ),

        # ✅ NEW: INN format validation (10-12 digits when present)
        CheckConstraint(
            "inn IS NULL OR inn ~ '^[0-9]{10,12}$'",
            name="ck_companies_inn_format"
        ),

        # ✅ NEW: UNIQUE partial indexes for phone_number and email
        # (allows multiple NULLs but enforces uniqueness when present)
        Index(
            "uq_companies_phone_number",
            "phone_number",
            unique=True,
            postgresql_where=text("phone_number IS NOT NULL")
        ),
        Index(
            "uq_companies_email",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL")
        ),
    )

    def __repr__(self):
        return f"<Company(id={self.id}, name='{self.name}', status={self.status})>"
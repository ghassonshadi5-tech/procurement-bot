"""
User model - represents people using the system.
Aligned with Procurement Bot Database Schema v2.0 (PostgreSQL 15+).

Each user belongs to either one company (buyer) OR one supplier, never both.
Roles are managed through the user_roles association table.

CHANGE LOG:
- Added phone_number field for contact-based supplier invitations.
- Imported UserStatus from enums.py (removed local duplicate).
- Changed phone_number type from String(20) to Text to match DB schema.
"""
from sqlalchemy import Column, String, BigInteger, ForeignKey, Enum, CheckConstraint, Text
from app.database.models.base import Base, IdentityMixin, TimestampMixin
from app.database.models.enums import UserStatus


class User(Base, IdentityMixin, TimestampMixin):
    """
    Represents a person using the procurement bot.
    A user can belong to a company (buyer) OR a supplier, but never both.
    """
    __tablename__ = "users"

    # Telegram user ID (unique identifier from Telegram)
    # IMPORTANT: Must be BigInteger because Telegram IDs can exceed Integer max value
    telegram_user_id = Column(
        BigInteger,
        nullable=False,
        unique=True,
        comment="Telegram user ID for authentication"
    )

    # User's full name
    full_name = Column(
        String(500),
        nullable=False,
        comment="User's full name"
    )

    # User's preferred language
    language = Column(
        String(2),
        nullable=False,
        default="ar",
        comment="User's preferred language (ar, ru, en)"
    )

    # ✅ Normalized phone number for contact-based supplier invitations
    # Format: digits only, no +, -, spaces (e.g., "79001234567")
    # Max 15 digits per E.164 standard + margin
    # Type: Text (matches DB schema; CheckConstraint enforces format)
    phone_number = Column(
        Text,
        nullable=True,
        index=True,
        comment="Normalized phone number for contact-based supplier invitations"
    )

    # Company association (nullable)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=True,
        index=True,
        comment="Company ID if user belongs to a buyer company"
    )

    # Supplier association (nullable)
    supplier_id = Column(
        BigInteger,
        ForeignKey("suppliers.id"),
        nullable=True,
        index=True,
        comment="Supplier ID if user belongs to a supplier company"
    )

    # User account status
    status = Column(
        Enum(UserStatus, name="user_status"),
        nullable=False,
        default=UserStatus.invited,
        comment="User account status"
    )

    __table_args__ = (
        # Prevent double affiliation: user cannot belong to both a company and a supplier
        CheckConstraint(
            "NOT (company_id IS NOT NULL AND supplier_id IS NOT NULL)",
            name="ck_users_no_double_affiliation"
        ),
        # Ensure language is one of the supported languages
        CheckConstraint(
            "language IN ('ar', 'ru', 'en')",
            name="ck_users_language"
        ),
        # ✅ Phone number must be digits only (7-15 digits) when present
        CheckConstraint(
            "phone_number IS NULL OR phone_number ~ '^[0-9]{7,15}$'",
            name="ck_users_phone_number_format"
        ),
    )

    def __repr__(self):
        return (
            f"<User(id={self.id}, telegram_user_id={self.telegram_user_id}, "
            f"phone={self.phone_number}, status={self.status})>"
        )
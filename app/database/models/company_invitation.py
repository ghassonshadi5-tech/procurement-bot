"""
CompanyInvitation model - represents invitations for users to join a company.
Aligned with Procurement Bot Database Schema v2.0.

Replaces the old role_invite_codes table.
Invitations are now token-based and linked to specific roles.
"""
from sqlalchemy import Column, String, BigInteger, ForeignKey, DateTime, CheckConstraint, func, text
from app.database.models.base import Base, IdentityMixin


class CompanyInvitation(Base, IdentityMixin):
    """
    Represents an invitation for a user to join a company with a specific role.
    
    Lifecycle:
    1. Authorized user creates an invitation with a role
    2. A secure token is generated (24 bytes hex = 48 chars)
    3. User receives the token (via deep link or code)
    4. User accepts the invitation before expiry
    5. Invitation is marked as accepted with accepted_by and accepted_at
    """
    __tablename__ = "company_invitations"

    # Company ID
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
        comment="Company ID"
    )

    # Role ID to be granted upon acceptance
    role_id = Column(
        BigInteger,
        ForeignKey("roles.id"),
        nullable=False,
        comment="Role ID to be granted upon acceptance"
    )

    # Secure token for the invitation
    token = Column(
        String(48),
        unique=True,
        nullable=False,
        server_default=text("encode(gen_random_bytes(24), 'hex')"),
        index=True,
        comment="Secure invitation token"
    )

    # Expiry timestamp
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="Timestamp when the invitation expires"
    )

    # Who accepted the invitation
    accepted_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who accepted the invitation"
    )

    # When the invitation was accepted
    accepted_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the invitation was accepted"
    )

    # Creation timestamp
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Creation timestamp"
    )

    __table_args__ = (
        # Expiry must be after creation
        CheckConstraint(
            "expires_at > created_at",
            name="ck_company_invitations_expiry_after_creation"
        ),
        # accepted_by and accepted_at must be set together or both NULL
        CheckConstraint(
            "(accepted_by IS NULL) = (accepted_at IS NULL)",
            name="ck_company_invitations_acceptance_consistency"
        ),
    )

    def __repr__(self):
        return f"<CompanyInvitation(id={self.id}, company_id={self.company_id}, role_id={self.role_id})>"
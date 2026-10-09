"""
UserRole model - associates users with roles.
Aligned with Procurement Bot Database Schema v2.0.

This is the many-to-many relationship table between users and roles.
The assert_role_scope() trigger in the database ensures role scope
matches user type (buyer roles for company users, supplier roles for
supplier users, platform roles for super admins).
"""
from sqlalchemy import Column, BigInteger, ForeignKey, DateTime, func
from app.database.models.base import Base


class UserRole(Base):
    """
    Associates a user with a role.
    Uses composite primary key (user_id, role_id).
    """
    __tablename__ = "user_roles"

    # User ID (part of composite primary key)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="User ID"
    )

    # Role ID (part of composite primary key)
    role_id = Column(
        BigInteger,
        ForeignKey("roles.id", ondelete="RESTRICT"),
        primary_key=True,
        comment="Role ID"
    )

    # Who granted this role
    granted_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who granted this role"
    )

    # When the role was granted
    granted_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when the role was granted"
    )

    def __repr__(self):
        return f"<UserRole(user_id={self.user_id}, role_id={self.role_id})>"
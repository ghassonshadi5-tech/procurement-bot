"""
Role model - represents system roles.
Aligned with Procurement Bot Database Schema v2.0.

The system has 9 roles divided into 3 scopes:
- Platform: super_admin
- Buyer: company_owner, manager, accountant, storekeeper, requester
- Supplier: supplier_owner, supplier_manager, supplier_employee

These roles are seeded into the database and referenced by user_roles.
"""
from sqlalchemy import Column, String, DateTime, Enum, func
from app.database.models.base import Base, IdentityMixin
import enum


class RoleScope(str, enum.Enum):
    """Role scope - determines which side the role belongs to."""
    platform = "platform"
    buyer = "buyer"
    supplier = "supplier"


class Role(Base, IdentityMixin):
    """
    Represents a system role.
    Roles are predefined and seeded into the database.
    """
    __tablename__ = "roles"

    # Unique role code (e.g., "super_admin", "company_owner")
    code = Column(
        String(50),
        unique=True,
        nullable=False,
        comment="Unique role code"
    )

    # Arabic name of the role
    name_ar = Column(
        String(200),
        nullable=False,
        comment="Arabic name of the role"
    )

    # Role scope
    scope = Column(
        Enum(RoleScope, name="role_scope"),
        nullable=False,
        comment="Role scope: platform, buyer, supplier"
    )

    # Creation timestamp
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Creation timestamp"
    )

    def __repr__(self):
        return f"<Role(id={self.id}, code={self.code}, scope={self.scope})>"
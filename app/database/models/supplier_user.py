"""
SupplierUser model - represents a user belonging to a supplier.
Similar to 'users' table (for tenants), but for suppliers.
"""
from sqlalchemy import Column, String, Boolean, BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from app.database.models.base import Base, SharedMixin


class SupplierUser(Base, SharedMixin):
    """User account for a supplier (owner, manager, employee)."""
    
    supplier_id = Column(
        UUID(as_uuid=True),
        ForeignKey("suppliers.id"),
        nullable=False,
        index=True,
        comment="The supplier this user belongs to"
    )
    
    telegram_id = Column(
        BigInteger,
        nullable=False,
        comment="Telegram user ID"
    )
    
    username = Column(String(255), nullable=True, comment="Telegram username")
    full_name = Column(String(500), nullable=True, comment="Full name")
    
    role = Column(
        String(50),
        nullable=False,
        comment="Role: owner, manager, employee"
    )
    
    is_active = Column(Boolean, default=True, nullable=False)
    email = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    
    __tablename__ = "supplier_users"
    __table_args__ = (
        UniqueConstraint('telegram_id', 'supplier_id', name='uq_supplier_user_telegram'),
    )
    
    def __repr__(self):
        return f"<SupplierUser(id={self.id}, telegram_id={self.telegram_id}, role={self.role})>"
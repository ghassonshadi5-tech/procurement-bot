"""
Attachment model - represents file attachments linked to business entities.
Aligned with Procurement Bot Database Schema v2.0.

Key rules:
- Attachments are linked polymorphically via (entity_table, entity_id).
- Belong to either a company OR a supplier (at least one must be set).
- Soft delete only (deleted_at), never physical deletion.
- RLS ensures company-scoped attachments are isolated.
"""
from sqlalchemy import (
    Column, String, Text, BigInteger, DateTime,
    ForeignKey, CheckConstraint, func
)
from app.database.models.base import Base, IdentityMixin


class Attachment(Base, IdentityMixin):
    """
    Represents a file attachment linked to a business entity.
    """
    __tablename__ = "attachments"

    # The table this attachment belongs to (polymorphic)
    entity_table = Column(
        String(100),
        nullable=False,
        comment="Entity table name"
    )

    # The ID of the entity this attachment belongs to
    entity_id = Column(
        BigInteger,
        nullable=False,
        index=True,
        comment="Entity ID"
    )

    # Company scope (RLS isolation)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=True,
        index=True,
        comment="Company ID (RLS isolation scope)"
    )

    # Supplier scope
    supplier_id = Column(
        BigInteger,
        ForeignKey("suppliers.id"),
        nullable=True,
        comment="Supplier ID"
    )

    # File metadata
    file_name = Column(
        String(500),
        nullable=False,
        comment="Original file name"
    )
    mime_type = Column(
        String(200),
        nullable=False,
        comment="MIME type"
    )
    size_bytes = Column(
        BigInteger,
        nullable=False,
        comment="File size in bytes"
    )

    # Storage reference
    storage_key = Column(
        String(500),
        unique=True,
        nullable=False,
        comment="Unique storage key"
    )
    checksum = Column(
        String(100),
        nullable=True,
        comment="File checksum for integrity"
    )

    # Who uploaded
    uploaded_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who uploaded"
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Creation timestamp"
    )
    deleted_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Soft delete timestamp"
    )

    __table_args__ = (
        # entity_table must be one of the allowed tables
        CheckConstraint(
            "entity_table IN ('purchase_requests', 'rfqs', 'quotations', "
            "'suppliers', 'companies', 'goods_receipts', 'items')",
            name="ck_attachments_entity_table"
        ),
        # Must belong to either a company or a supplier
        CheckConstraint(
            "company_id IS NOT NULL OR supplier_id IS NOT NULL",
            name="ck_attachments_owner_required"
        ),
        # Size must be positive
        CheckConstraint(
            "size_bytes > 0",
            name="ck_attachments_size_positive"
        ),
    )

    def __repr__(self):
        return (
            f"<Attachment(id={self.id}, entity_table={self.entity_table}, "
            f"entity_id={self.entity_id}, file_name={self.file_name})>"
        )
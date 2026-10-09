"""
AuditLog model - general audit trail for all sensitive operations.
Aligned with Procurement Bot Database Schema v2.0.

CRITICAL: This table is Append-Only. The forbid_mutation() trigger
prevents any UPDATE or DELETE.

Populated automatically by audit_trigger() on sensitive tables,
and explicitly by Services for business decisions (approve, reject, award).
"""
from sqlalchemy import (
    Column, String, Text, BigInteger, DateTime,
    ForeignKey, Enum, func
)
from sqlalchemy.dialects.postgresql import JSONB
from app.database.models.base import Base, IdentityMixin
from app.database.models.enums import AuditAction


class AuditLog(Base, IdentityMixin):
    """
    General audit log (Append-Only).
    Records all sensitive operations with full before/after snapshots.
    """
    __tablename__ = "audit_log"

    # Entity being audited
    entity_table = Column(
        String(100),
        nullable=False,
        comment="Entity table name"
    )
    entity_id = Column(
        BigInteger,
        nullable=False,
        index=True,
        comment="Entity ID"
    )

    # Action performed
    action = Column(
        Enum(AuditAction, name="audit_action"),
        nullable=False,
        comment="Action type"
    )

    # Who performed the action
    actor_user_id = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who performed the action"
    )
    actor_role = Column(
        String(50),
        nullable=True,
        comment="Role of the actor at the time"
    )

    # Company scope (RLS isolation)
    company_id = Column(
        BigInteger,
        nullable=True,
        index=True,
        comment="Company ID (RLS isolation scope)"
    )

    # Before/after snapshots
    old_data = Column(
        JSONB,
        nullable=True,
        comment="Full snapshot before change"
    )
    new_data = Column(
        JSONB,
        nullable=True,
        comment="Full snapshot after change"
    )

    # Reason (for rejections, cancellations, awards)
    reason = Column(
        Text,
        nullable=True,
        comment="Reason for the action"
    )

    # Source Telegram update (for traceability)
    telegram_update_id = Column(
        BigInteger,
        nullable=True,
        comment="Source Telegram update ID"
    )

    # When the action occurred
    occurred_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when the action occurred"
    )

    def __repr__(self):
        return (
            f"<AuditLog(id={self.id}, entity_table={self.entity_table}, "
            f"entity_id={self.entity_id}, action={self.action})>"
        )
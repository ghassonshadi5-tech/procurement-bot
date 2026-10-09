"""
Notification model - represents user notifications.
Aligned with Procurement Bot Database Schema v2.0.

Notifications are informational only — they do NOT change business state.
State changes always go through the authorized Service layer.
"""
from sqlalchemy import (
    Column, String, BigInteger, DateTime, ForeignKey,
    CheckConstraint, func, text
)
from sqlalchemy.dialects.postgresql import JSONB
from app.database.models.base import Base, IdentityMixin


class Notification(Base, IdentityMixin):
    """
    Represents a notification sent to a user.
    """
    __tablename__ = "notifications"

    # Target user
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Target user ID"
    )

    # Company scope (optional, for filtering)
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=True,
        index=True,
        comment="Company ID for scoping"
    )

    # Notification kind/type
    kind = Column(
        String(100),
        nullable=False,
        comment="Notification kind (e.g., rfq_published, request_approved)"
    )

    # Notification payload
    payload = Column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Notification payload data"
    )

    # When sent
    sent_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when sent"
    )

    # When read
    read_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when read by user"
    )

    # Creation timestamp
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Creation timestamp"
    )

    __table_args__ = (
        # Payload must be a JSON object
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_notifications_payload_is_object"
        ),
    )

    def __repr__(self):
        return (
            f"<Notification(id={self.id}, user_id={self.user_id}, "
            f"kind={self.kind})>"
        )
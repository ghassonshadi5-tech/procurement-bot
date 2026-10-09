"""
RfqSupplier model - tracks which suppliers are invited to an RFQ.
Aligned with Procurement Bot Database Schema v2.0.

Uses composite primary key (rfq_id, supplier_id) since a supplier
can only be invited once per RFQ.

CHANGE LOG:
- Imported InviteChannel and InviteStatus from enums.py (Single Source of Truth).
- Kept invite_channel as String(20) to match DB schema (varchar, not enum).
- Added __all__ for backward compatibility.
"""
from sqlalchemy import (
    Column, String, BigInteger, DateTime, ForeignKey,
    Enum, CheckConstraint, func
)
from app.database.models.base import Base
from app.database.models.enums import InviteChannel, InviteStatus

# Re-export for backward compatibility
__all__ = ["RfqSupplier", "InviteChannel", "InviteStatus"]


class RfqSupplier(Base):
    """
    Tracks which suppliers are invited to an RFQ and their response status.
    """
    __tablename__ = "rfq_suppliers"

    # Parent RFQ (part of composite PK)
    rfq_id = Column(
        BigInteger,
        ForeignKey("rfqs.id", ondelete="CASCADE"),
        primary_key=True,
        comment="RFQ ID"
    )

    # Invited supplier (part of composite PK)
    supplier_id = Column(
        BigInteger,
        ForeignKey("suppliers.id"),
        primary_key=True,
        comment="Invited supplier ID"
    )

    # How the supplier was invited
    # NOTE: DB column is varchar(20), not PostgreSQL enum.
    # InviteChannel enum is used for default value only.
    invite_channel = Column(
        String(20),
        nullable=False,
        default=InviteChannel.targeted.value,
        comment="Invite channel: targeted or open"
    )

    # Supplier's response status
    invitation_status = Column(
        Enum(InviteStatus, name="invite_status"),
        nullable=False,
        default=InviteStatus.invited,
        comment="Invitation status"
    )

    # When the invitation was sent
    invited_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when the invitation was sent"
    )

    # When the supplier responded
    responded_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the supplier responded"
    )

    __table_args__ = (
        # invite_channel must be targeted or open
        CheckConstraint(
            "invite_channel IN ('targeted', 'open')",
            name="ck_rfq_suppliers_invite_channel"
        ),
    )

    def __repr__(self):
        return (
            f"<RfqSupplier(rfq_id={self.rfq_id}, "
            f"supplier_id={self.supplier_id}, "
            f"status={self.invitation_status})>"
        )
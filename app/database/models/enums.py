"""
Shared enums for database models.
Aligned with Procurement Bot Database Schema v2.0.

Centralizing enums here prevents circular imports and ensures consistency
across all models that reference the same PostgreSQL enum types.

CHANGE LOG:
- Added InviteChannel (for rfq_suppliers.invite_channel — kept as String in DB).
- Added InviteStatus (for rfq_suppliers.invitation_status).
"""
import enum


class RoleScope(str, enum.Enum):
    """Role scope: platform, buyer, supplier."""
    platform = "platform"
    buyer = "buyer"
    supplier = "supplier"


class CompanyStatus(str, enum.Enum):
    """Company/Supplier registration status."""
    pending = "pending"
    active = "active"
    suspended = "suspended"
    rejected = "rejected"


class UserStatus(str, enum.Enum):
    """User account status."""
    invited = "invited"
    active = "active"
    blocked = "blocked"


class RelationStatus(str, enum.Enum):
    """Company-Supplier relationship status."""
    pending = "pending"
    active = "active"
    suspended = "suspended"
    rejected = "rejected"


class PriceType(str, enum.Enum):
    """Price type: public or custom."""
    public = "public"
    custom = "custom"


class PriceStatus(str, enum.Enum):
    """Price rule status."""
    draft = "draft"
    active = "active"
    inactive = "inactive"
    expired = "expired"


class RequestStatus(str, enum.Enum):
    """Purchase request status — full lifecycle."""
    draft = "draft"
    submitted = "submitted"
    under_review = "under_review"
    needs_revision = "needs_revision"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"
    rfq_created = "rfq_created"
    awarded = "awarded"
    received = "received"
    closed = "closed"
    archived = "archived"


class AuditAction(str, enum.Enum):
    """Audit action types for logging."""
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    AWARD = "AWARD"
    STATUS_CHANGE = "STATUS_CHANGE"


class RfqMethod(str, enum.Enum):
    """RFQ method: targeted, open, or mixed."""
    targeted = "targeted"
    open = "open"
    mixed = "mixed"


class RfqStatus(str, enum.Enum):
    """RFQ status per DB Schema v2.0."""
    draft = "draft"
    published = "published"
    closed = "closed"
    cancelled = "cancelled"
    awarded = "awarded"


class QuotationStatus(str, enum.Enum):
    """Quotation status per DB Schema v2.0."""
    draft = "draft"
    submitted = "submitted"
    late = "late"
    withdrawn = "withdrawn"
    accepted = "accepted"
    rejected = "rejected"
    not_selected = "not_selected"


class InviteChannel(str, enum.Enum):
    """
    How the supplier was invited to an RFQ.
    NOTE: In DB, rfq_suppliers.invite_channel is varchar(20) with CHECK,
    not a PostgreSQL enum. This Python enum is used for application-level
    consistency only.
    """
    targeted = "targeted"
    open = "open"


class InviteStatus(str, enum.Enum):
    """Supplier's response status to the RFQ invitation."""
    invited = "invited"
    viewed = "viewed"
    responded = "responded"
    declined = "declined"
    expired = "expired"
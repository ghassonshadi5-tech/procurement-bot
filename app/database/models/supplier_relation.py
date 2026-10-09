"""
SupplierRelation model - links a supplier to a buyer company.
Aligned with Procurement Bot Database Schema v2.0 (PostgreSQL 15+).

This is the many-to-many relationship table between companies and suppliers.
Each row represents: "Company X has a relationship with Supplier Y"

Key rules from the schema:
- A supplier can be linked to multiple companies.
- A company can have multiple suppliers.
- Only ONE relationship record per (company, supplier) pair.
- Suspending a relation MUST have a reason.
- Suspending prevents new operations but preserves history.

CHANGE LOG:
- Imported RelationStatus from enums.py (Single Source of Truth) with re-export for backward compatibility.
- Removed unused `import enum`.
"""
from sqlalchemy import (
    Column, Text, BigInteger, ForeignKey,
    Enum, CheckConstraint, UniqueConstraint, DateTime
)
from app.database.models.base import Base, IdentityMixin
from app.database.models.enums import RelationStatus

# Re-export RelationStatus for backward compatibility with existing imports
__all__ = ["SupplierRelation", "RelationStatus"]


class SupplierRelation(Base, IdentityMixin):
    """
    Links a supplier to a buyer company with relationship status.

    Business rules:
    - Only one relation per (company_id, supplier_id) pair.
    - decided_by and decided_at must be set together (or both NULL).
    - If status is 'suspended', status_reason must be provided.
    - Suspending prevents new operations but does NOT delete history.
    """

    __tablename__ = "supplier_relations"

    # Company (buyer) ID
    company_id = Column(
        BigInteger,
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
        comment="The buyer company ID"
    )

    # Supplier ID
    supplier_id = Column(
        BigInteger,
        ForeignKey("suppliers.id"),
        nullable=False,
        index=True,
        comment="The supplier ID"
    )

    # Relationship status
    status = Column(
        Enum(RelationStatus, name="relation_status"),
        nullable=False,
        default=RelationStatus.pending,
        comment="Relationship status"
    )

    # Who decided (approved/rejected/suspended) the relationship
    decided_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        comment="User ID who made the decision on this relationship"
    )

    # When the decision was made
    decided_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the decision was made"
    )

    # Reason for the current status (mandatory for 'suspended')
    status_reason = Column(
        Text,
        nullable=True,
        comment="Reason for suspension/rejection"
    )

    __table_args__ = (
        # Only one relationship per (company, supplier) pair
        UniqueConstraint(
            "company_id", "supplier_id",
            name="uq_supplier_relations_company_supplier"
        ),
        # decided_by and decided_at must be set together or both NULL
        CheckConstraint(
            "(decided_by IS NULL) = (decided_at IS NULL)",
            name="ck_supplier_relations_decision_consistency"
        ),
        # If status is 'suspended', a reason must be provided
        CheckConstraint(
            "status <> 'suspended' OR btrim(status_reason) <> ''",
            name="ck_supplier_relations_suspension_reason"
        ),
    )

    def __repr__(self):
        return (
            f"<SupplierRelation(id={self.id}, "
            f"company_id={self.company_id}, "
            f"supplier_id={self.supplier_id}, "
            f"status={self.status})>"
        )
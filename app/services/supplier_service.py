"""
Supplier Service - Business logic for managing suppliers and their relations.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Supplier lifecycle (register as pending, approve, reject, suspend).
- Supplier-Company relationships (SupplierRelation).
- Fetching supplier details and users.

NOTE: In the new architecture:
- There is no separate `supplier_requests` table. Registration is done
  directly in the `suppliers` table with status='pending'.
- There is no `supplier_users` table. All users are in the global `users`
  table with a `supplier_id` FK.
- There is no `supplier_catalog` table. Items belong to suppliers via
  `items.supplier_id`.

IMPROVEMENTS (this revision):
- All unscoped sessions use silent=True (supplier lookups are
  bootstrap/administrative and do not require tenant RLS).
- reject_supplier() accepts an optional reason, recorded in audit_log
  (suppliers has no rejected_by column, so the actor is preserved via
  audit_service).
- suspend_supplier() accepts suspended_by_user_id and reason (both
  optional for backward compatibility), recorded in audit_log.
- approve_supplier() records an explicit audit entry with the actor
  (suppliers has no approved_by column).
- create_registration() warns when it unlinks a previous company
  affiliation (previously silent).
- get_supplier_relations() requires caller_supplier_id and enforces
  that the caller is the target supplier, preventing cross-supplier
  leaks.
- CompanyStatus imported from company module (correct origin).
- Platform-level audit events (supplier approval/rejection/suspension)
  use company_id=None instead of a placeholder integer.
- Uses get_logger(__name__) and %s formatting.
"""
from typing import Optional, List
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.supplier import Supplier
from app.database.models.company import CompanyStatus
from app.database.models.supplier_relation import SupplierRelation, RelationStatus
from app.database.models.user import User
from app.database.models.enums import AuditAction
from app.core.logger import get_logger
from app.services.audit_service import audit_service


logger = get_logger(__name__)


class SupplierService:
    """
    Service class for Supplier-related operations.
    """

    # ========================================================================
    # 1. REGISTRATION & LIFECYCLE
    # ========================================================================

    @staticmethod
    async def create_registration(
        name: str,
        contact_user_id: int,
        inn: Optional[str] = None,
        activity: Optional[str] = None,
        cities: Optional[List[str]] = None,
        contact_email: Optional[str] = None,
    ) -> Supplier:
        """
        Create a new supplier registration request.
        The supplier is created with status='pending' and requires Super
        Admin approval.

        The contact user is linked to this supplier. If the user was
        previously affiliated with a company, the affiliation is removed
        and a warning is logged (previously silent).

        The INSERT on suppliers and the UPDATE on users are captured by
        audit_trigger().
        """
        async with get_session(silent=True) as session:
            new_supplier = Supplier(
                name=name,
                inn=inn,
                activity=activity,
                cities=cities or [],
                contact_email=contact_email,
                status=CompanyStatus.pending,
                is_verified=False,
            )
            session.add(new_supplier)
            await session.flush()  # Get the ID

            # Link the contact user to this supplier
            stmt = select(User).where(User.id == contact_user_id)
            user = (await session.execute(stmt)).scalar_one_or_none()

            if user:
                if user.company_id is not None:
                    logger.warning(
                        "Supplier registration unlinks previous company affiliation"
                        " | user_id=%s | previous_company_id=%s | new_supplier_id=%s",
                        contact_user_id,
                        user.company_id,
                        new_supplier.id,
                    )
                user.supplier_id = new_supplier.id
                # Ensure no double affiliation
                user.company_id = None

            await session.commit()
            await session.refresh(new_supplier)

            logger.info(
                "Created pending supplier | supplier_id=%s | name=%s | by_user=%s",
                new_supplier.id,
                new_supplier.name,
                contact_user_id,
            )
            return new_supplier

    @staticmethod
    async def get_supplier_by_id(supplier_id: int) -> Optional[Supplier]:
        """
        Retrieve a supplier by its ID.

        Uses silent=True: lookup by primary key, no tenant RLS needed.
        """
        async with get_session(silent=True) as session:
            stmt = select(Supplier).where(Supplier.id == supplier_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    @staticmethod
    async def get_user_supplier(user_id: int) -> Optional[Supplier]:
        """
        Get the supplier that a specific user belongs to.
        Returns None if the user is not affiliated with any supplier.

        Uses silent=True: user_id identifies the row directly.
        """
        async with get_session(silent=True) as session:
            stmt = (
                select(Supplier)
                .join(User, User.supplier_id == Supplier.id)
                .where(User.id == user_id)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    @staticmethod
    async def approve_supplier(
        supplier_id: int,
        approved_by_user_id: int,
    ) -> bool:
        """
        Approve a pending supplier registration (Super Admin action).
        Changes status to 'active' and marks as verified.

        The suppliers table has no approved_by column, so an explicit
        audit entry records the actor. company_id is None because this
        is a platform-level event.
        """
        async with get_session(silent=True) as session:
            stmt = select(Supplier).where(Supplier.id == supplier_id)
            supplier = (await session.execute(stmt)).scalar_one_or_none()

            if not supplier or supplier.status != CompanyStatus.pending:
                logger.warning(
                    "Cannot approve supplier | supplier_id=%s "
                    "| reason=not_found_or_not_pending",
                    supplier_id,
                )
                return False

            old_status = supplier.status.value
            supplier.status = CompanyStatus.active
            supplier.is_verified = True
            supplier.verified_at = datetime.now(timezone.utc)

            await audit_service.log_event(
                company_id=None,  # Platform-level event (no tenant scope)
                entity_table="suppliers",
                entity_id=supplier.id,
                action=AuditAction.APPROVE,
                actor_user_id=approved_by_user_id,
                old_data={"status": old_status, "is_verified": False},
                new_data={
                    "status": CompanyStatus.active.value,
                    "is_verified": True,
                },
                reason="Supplier registration approved",
                session=session,
            )

            await session.commit()

            logger.info(
                "Supplier approved | supplier_id=%s | name=%s | by_user=%s",
                supplier.id,
                supplier.name,
                approved_by_user_id,
            )
            return True

    @staticmethod
    async def reject_supplier(
        supplier_id: int,
        approved_by_user_id: int,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Reject a pending supplier registration (Super Admin action).
        Changes status to 'rejected'.

        The reason and actor are recorded in audit_log (suppliers has no
        rejected_by or rejection_reason column). company_id is None
        because this is a platform-level event.
        """
        async with get_session(silent=True) as session:
            stmt = select(Supplier).where(Supplier.id == supplier_id)
            supplier = (await session.execute(stmt)).scalar_one_or_none()

            if not supplier or supplier.status != CompanyStatus.pending:
                return False

            old_status = supplier.status.value
            supplier.status = CompanyStatus.rejected

            await audit_service.log_event(
                company_id=None,  # Platform-level event
                entity_table="suppliers",
                entity_id=supplier.id,
                action=AuditAction.REJECT,
                actor_user_id=approved_by_user_id,
                old_data={"status": old_status},
                new_data={"status": CompanyStatus.rejected.value},
                reason=reason or "Supplier registration rejected",
                session=session,
            )

            await session.commit()

            logger.info(
                "Supplier rejected | supplier_id=%s | name=%s | by_user=%s",
                supplier.id,
                supplier.name,
                approved_by_user_id,
            )
            return True

    @staticmethod
    async def suspend_supplier(
        supplier_id: int,
        suspended_by_user_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Suspend an active supplier (prevents new operations).

        The suspension is recorded in audit_log (suppliers has no
        suspended_by column). Both parameters are optional for backward
        compatibility with any existing callers. company_id is None
        because this is a platform-level event.
        """
        async with get_session(silent=True) as session:
            stmt = select(Supplier).where(Supplier.id == supplier_id)
            supplier = (await session.execute(stmt)).scalar_one_or_none()

            if not supplier or supplier.status != CompanyStatus.active:
                return False

            old_status = supplier.status.value
            supplier.status = CompanyStatus.suspended

            await audit_service.log_event(
                company_id=None,  # Platform-level event
                entity_table="suppliers",
                entity_id=supplier.id,
                action=AuditAction.STATUS_CHANGE,
                actor_user_id=suspended_by_user_id,
                actor_role="SYSTEM" if suspended_by_user_id is None else None,
                old_data={"status": old_status},
                new_data={"status": CompanyStatus.suspended.value},
                reason=reason or "Supplier suspended",
                session=session,
            )

            await session.commit()

            logger.warning(
                "Supplier SUSPENDED | supplier_id=%s | name=%s | by_user=%s",
                supplier.id,
                supplier.name,
                suspended_by_user_id,
            )
            return True

    @staticmethod
    async def list_all_suppliers(
        status: Optional[CompanyStatus] = None,
    ) -> List[Supplier]:
        """
        List all suppliers in the system (Super Admin view).
        Optionally filter by status.

        Uses silent=True: intentionally unscoped (platform admin).
        """
        async with get_session(silent=True) as session:
            stmt = select(Supplier)
            if status:
                stmt = stmt.where(Supplier.status == status)
            stmt = stmt.order_by(Supplier.created_at.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ========================================================================
    # 2. SUPPLIER-COMPANY RELATIONS
    # ========================================================================

    @staticmethod
    async def create_relation(
        company_id: int,
        supplier_id: int,
        decided_by_user_id: Optional[int] = None,
        auto_approve: bool = False,
    ) -> Optional[SupplierRelation]:
        """
        Create a relationship between a company and a supplier.

        If auto_approve=True, status is 'active' immediately.
        Otherwise, status is 'pending' and requires supplier acceptance.

        The INSERT on supplier_relations is captured by audit_trigger().
        """
        async with get_session(company_id=company_id) as session:
            # Check if relation already exists
            stmt = select(SupplierRelation).where(
                SupplierRelation.company_id == company_id,
                SupplierRelation.supplier_id == supplier_id,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                logger.warning(
                    "Relation already exists | company_id=%s | supplier_id=%s",
                    company_id,
                    supplier_id,
                )
                return None

            status = (
                RelationStatus.active if auto_approve else RelationStatus.pending
            )

            relation = SupplierRelation(
                company_id=company_id,
                supplier_id=supplier_id,
                status=status,
                decided_by=decided_by_user_id,
                decided_at=(
                    datetime.now(timezone.utc) if auto_approve else None
                ),
            )

            session.add(relation)
            await session.commit()
            await session.refresh(relation)

            logger.info(
                "Created relation | company_id=%s | supplier_id=%s | status=%s",
                company_id,
                supplier_id,
                status.value,
            )
            return relation

    @staticmethod
    async def get_supplier_relations(
        supplier_id: int,
        caller_supplier_id: int,
        status: Optional[RelationStatus] = None,
    ) -> List[SupplierRelation]:
        """
        Get all company relations for a specific supplier.

        SECURITY: caller_supplier_id must equal supplier_id. This prevents
        a supplier from listing other suppliers' company relations. The
        check is enforced here even if the caller is a handler.

        Uses silent=True because the query intentionally spans multiple
        companies (relations are the join between tenants).
        """
        # --- Enforce caller scope ---
        if caller_supplier_id != supplier_id:
            logger.warning(
                "Cross-supplier relation lookup denied"
                " | caller_supplier_id=%s | target_supplier_id=%s",
                caller_supplier_id,
                supplier_id,
                extra={"security": True},
            )
            return []

        async with get_session(silent=True) as session:
            stmt = select(SupplierRelation).where(
                SupplierRelation.supplier_id == supplier_id
            )
            if status:
                stmt = stmt.where(SupplierRelation.status == status)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    async def get_company_suppliers(
        company_id: int,
        status: Optional[RelationStatus] = None,
    ) -> List[SupplierRelation]:
        """
        Get all supplier relations for a specific company.

        Uses company_id-scoped session (RLS enforced by the caller).
        """
        async with get_session(company_id=company_id) as session:
            stmt = select(SupplierRelation).where(
                SupplierRelation.company_id == company_id
            )
            if status:
                stmt = stmt.where(SupplierRelation.status == status)

            result = await session.execute(stmt)
            return list(result.scalars().all())


# Singleton instance for easy import
supplier_service = SupplierService()
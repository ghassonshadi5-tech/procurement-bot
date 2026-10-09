"""
RFQ Service - Business logic for managing Request For Quotations.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Create RFQs from approved purchase requests.
- Manage RFQ lifecycle (draft -> published -> closed/cancelled -> awarded).
- Invite suppliers (targeted) or manage open/mixed RFQs.
- Enforce deadline rules and state machine transitions.
- All DB operations are async and respect RLS context (company_id).

Key Rules from Lifecycle Document:
- RFQ can only be created from an APPROVED purchase request.
- Publishing sets the deadline.
- Closing the deadline does NOT auto-select a winner (human decision required).
- Cancellation is allowed in draft/published states.

IMPROVEMENTS (this revision):
- publish_rfq() now accepts actor_user_id (for audit) and bot (optional,
  for supplier notification after commit).
- close_rfq() accepts actor_user_id (for audit).
- close_rfq_system() added: closes an RFQ without a company scope, for
  the scheduler.
- cancel_rfq() accepts reason (recorded in audit_log, not in the table).
- Every state transition writes an audit_log entry INSIDE the same
  transaction as the state change (atomic).
- Notifications are sent AFTER commit, wrapped in try/except so a
  notification failure never rolls back a successful state change.
- Logging uses %s formatting.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.rfq import Rfq
from app.database.models.rfq_line import RfqLine
from app.database.models.rfq_supplier import RfqSupplier
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.purchase_request_line import PurchaseRequestLine
from app.database.models.enums import (
    RfqMethod,
    RfqStatus,
    RequestStatus,
    AuditAction,
)
from app.core.logger import get_logger
from app.services.audit_service import audit_service


logger = get_logger(__name__)


class RfqService:
    """
    Service class for RFQ operations.
    """

    # ========================================================================
    # 1. CREATE RFQ FROM APPROVED REQUEST
    # ========================================================================

    @staticmethod
    async def create_rfq_from_request(
        company_id: int,
        request_id: int,
        created_by: int,
        method: RfqMethod = RfqMethod.targeted,
    ) -> Optional[Rfq]:
        """
        Create a new RFQ draft from an approved purchase request.
        Transitions the purchase request to 'rfq_created' status.

        NOTE: No explicit audit entry is written here — the PostgreSQL
        audit_trigger() captures both the INSERT on rfqs and the UPDATE
        on purchase_requests automatically.
        """
        async with get_session(company_id=company_id) as session:
            # 1. Fetch the purchase request
            stmt = select(PurchaseRequest).where(
                PurchaseRequest.id == request_id
            )
            request = (await session.execute(stmt)).scalar_one_or_none()

            if not request:
                logger.warning(
                    "Request %s not found for company %s",
                    request_id,
                    company_id,
                )
                return None

            # 2. Validate state transition (must be APPROVED)
            if request.status != RequestStatus.approved:
                logger.warning(
                    "Cannot create RFQ: Request %s is not approved (current: %s)",
                    request_id,
                    request.status.value,
                )
                return None

            # 3. Create RFQ header
            new_rfq = Rfq(
                company_id=company_id,
                request_id=request_id,
                method=method,
                status=RfqStatus.draft,
                created_by=created_by,
            )
            session.add(new_rfq)
            await session.flush()  # Get new_rfq.id

            # 4. Copy lines from purchase request to RFQ
            stmt_lines = select(PurchaseRequestLine).where(
                PurchaseRequestLine.request_id == request_id
            )
            req_lines = (await session.execute(stmt_lines)).scalars().all()

            for req_line in req_lines:
                rfq_line = RfqLine(
                    rfq_id=new_rfq.id,
                    request_line_id=req_line.id,
                    line_no=req_line.line_no,
                    quantity=req_line.quantity,
                    uom_id=req_line.uom_id,
                )
                session.add(rfq_line)

            # 5. Update Purchase Request status to 'rfq_created'
            request.status = RequestStatus.rfq_created

            await session.commit()
            await session.refresh(new_rfq)

            logger.info(
                "Created RFQ %s from request %s (method: %s)",
                new_rfq.id,
                request_id,
                method.value,
            )
            return new_rfq

    # ========================================================================
    # 2. INVITE SUPPLIERS
    # ========================================================================

    @staticmethod
    async def invite_suppliers(
        company_id: int,
        rfq_id: int,
        supplier_ids: List[int],
    ) -> int:
        """
        Add targeted suppliers to the RFQ invitation list.
        Returns the number of successfully invited suppliers.
        """
        async with get_session(company_id=company_id) as session:
            # Verify RFQ exists and is in draft state (can't invite after closing)
            stmt = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt)).scalar_one_or_none()

            if not rfq or rfq.status != RfqStatus.draft:
                return 0

            invited_count = 0
            for sup_id in supplier_ids:
                stmt_check = select(RfqSupplier).where(
                    RfqSupplier.rfq_id == rfq_id,
                    RfqSupplier.supplier_id == sup_id,
                )
                exists = (await session.execute(stmt_check)).scalar_one_or_none()

                if not exists:
                    invitation = RfqSupplier(
                        rfq_id=rfq_id,
                        supplier_id=sup_id,
                        invite_channel="targeted",
                        invitation_status="invited",
                    )
                    session.add(invitation)
                    invited_count += 1

            await session.commit()
            logger.info("Invited %s suppliers to RFQ %s", invited_count, rfq_id)
            return invited_count

    # ========================================================================
    # 3. PUBLISH RFQ
    # ========================================================================

    @staticmethod
    async def publish_rfq(
        company_id: int,
        rfq_id: int,
        deadline_hours: int,
        actor_user_id: int,
        bot: Optional[Any] = None,
    ) -> bool:
        """
        Publish the RFQ and set the deadline.
        Transitions state from draft to published.

        Writes an audit entry INSIDE the same transaction as the state
        change. Sends supplier notifications AFTER commit (best-effort —
        a notification failure does not roll back the publish).

        Args:
            company_id: Tenant scope.
            rfq_id: RFQ to publish.
            deadline_hours: Competition window length in hours.
            actor_user_id: The user performing the action (for audit).
            bot: Optional Aiogram Bot instance. If provided, invited
                 suppliers are notified after a successful publish.
        """
        # Snapshot of supplier_ids to notify after commit
        notify_supplier_ids: List[int] = []
        published_deadline: Optional[datetime] = None
        lines_count = 0

        async with get_session(company_id=company_id) as session:
            stmt = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt)).scalar_one_or_none()

            if not rfq:
                return False

            if rfq.status != RfqStatus.draft:
                logger.warning(
                    "Cannot publish RFQ %s: not in draft state.", rfq_id
                )
                return False

            # For targeted RFQs, ensure at least one supplier is invited
            if rfq.method == RfqMethod.targeted:
                stmt_sup = select(RfqSupplier).where(
                    RfqSupplier.rfq_id == rfq_id
                )
                suppliers = (await session.execute(stmt_sup)).scalars().all()
                if not suppliers:
                    logger.warning(
                        "Cannot publish targeted RFQ %s: no suppliers invited.",
                        rfq_id,
                    )
                    return False
                notify_supplier_ids = [s.supplier_id for s in suppliers]

            # Count lines (for notification payload)
            stmt_lines = select(RfqLine).where(RfqLine.rfq_id == rfq_id)
            lines = (await session.execute(stmt_lines)).scalars().all()
            lines_count = len(lines)

            now = datetime.now(timezone.utc)
            deadline = now + timedelta(hours=deadline_hours)

            old_status = rfq.status.value
            rfq.status = RfqStatus.published
            rfq.published_at = now
            rfq.deadline = deadline

            # --- Audit entry (same transaction) ---
            await audit_service.log_event(
                company_id=company_id,
                entity_table="rfqs",
                entity_id=rfq.id,
                action=AuditAction.STATUS_CHANGE,
                actor_user_id=actor_user_id,
                old_data={"status": old_status},
                new_data={
                    "status": RfqStatus.published.value,
                    "published_at": now.isoformat(),
                    "deadline": deadline.isoformat(),
                    "deadline_hours": deadline_hours,
                },
                reason="RFQ published",
                session=session,
            )

            await session.commit()
            published_deadline = deadline

        logger.info(
            "Published RFQ %s with deadline at %s (actor=%s)",
            rfq_id,
            published_deadline,
            actor_user_id,
        )

        # --- Notifications (after commit — best effort) ---
        if bot is not None and notify_supplier_ids and published_deadline:
            try:
                from app.services.notification_service import notification_service
                await notification_service.notify_rfq_published_to_suppliers(
                    bot=bot,
                    rfq_id=rfq_id,
                    company_id=company_id,
                    supplier_ids=notify_supplier_ids,
                    deadline=published_deadline,
                    lines_count=lines_count,
                )
            except Exception:
                logger.exception(
                    "Failed to notify suppliers for RFQ %s", rfq_id
                )

        return True

    # ========================================================================
    # 4. CLOSE RFQ (user-scoped)
    # ========================================================================

    @staticmethod
    async def close_rfq(
        company_id: int,
        rfq_id: int,
        actor_user_id: Optional[int] = None,
    ) -> bool:
        """
        Close the RFQ (manually). Transitions state to 'closed'.
        Does NOT select a winner.
        """
        async with get_session(company_id=company_id) as session:
            stmt = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt)).scalar_one_or_none()

            if not rfq or rfq.status != RfqStatus.published:
                return False

            old_status = rfq.status.value
            now = datetime.now(timezone.utc)
            rfq.status = RfqStatus.closed
            rfq.closed_at = now

            await audit_service.log_event(
                company_id=company_id,
                entity_table="rfqs",
                entity_id=rfq.id,
                action=AuditAction.STATUS_CHANGE,
                actor_user_id=actor_user_id,
                old_data={"status": old_status},
                new_data={"status": RfqStatus.closed.value},
                reason="RFQ closed manually",
                session=session,
            )

            await session.commit()

        logger.info("Closed RFQ %s (actor=%s)", rfq_id, actor_user_id)
        return True

    # ========================================================================
    # 5. CLOSE RFQ (system — for scheduler)
    # ========================================================================

    @staticmethod
    async def close_rfq_system(
        rfq_id: int,
        bot: Optional[Any] = None,
    ) -> bool:
        """
        Close an RFQ on behalf of the scheduler (no user context).

        - Uses silent=True (system operation, no tenant scope available
          before the RFQ row is read).
        - Writes an audit entry with actor_user_id=None and
          actor_role="SYSTEM".
        - If bot is provided, notifies company managers after commit.
        """
        company_id_for_notify: Optional[int] = None
        quotations_count = 0

        async with get_session(silent=True) as session:
            stmt = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt)).scalar_one_or_none()

            if not rfq or rfq.status != RfqStatus.published:
                return False

            # Count submitted quotations (for the notification payload)
            from app.database.models.quotation import Quotation
            from app.database.models.enums import QuotationStatus

            stmt_count = select(Quotation).where(
                Quotation.rfq_id == rfq.id,
                Quotation.status == QuotationStatus.submitted,
            )
            quotations = (await session.execute(stmt_count)).scalars().all()
            quotations_count = len(quotations)

            old_status = rfq.status.value
            now = datetime.now(timezone.utc)
            rfq.status = RfqStatus.closed
            rfq.closed_at = now
            company_id_for_notify = rfq.company_id

            await audit_service.log_event(
                company_id=rfq.company_id,
                entity_table="rfqs",
                entity_id=rfq.id,
                action=AuditAction.STATUS_CHANGE,
                actor_user_id=None,
                actor_role="SYSTEM",
                old_data={"status": old_status},
                new_data={
                    "status": RfqStatus.closed.value,
                    "closed_by": "scheduler",
                },
                reason="Deadline reached (auto-close by scheduler)",
                session=session,
            )

            await session.commit()

        logger.info(
            "Auto-closed RFQ %s (deadline reached, %s quotations)",
            rfq_id,
            quotations_count,
        )

        # --- Notifications (after commit — best effort) ---
        if bot is not None and company_id_for_notify is not None:
            try:
                from app.services.notification_service import notification_service
                await notification_service.notify_rfq_deadline_reached(
                    bot=bot,
                    rfq_id=rfq_id,
                    company_id=company_id_for_notify,
                    quotations_count=quotations_count,
                )
            except Exception:
                logger.exception(
                    "Failed to notify managers about RFQ %s deadline", rfq_id
                )

        return True

    # ========================================================================
    # 6. CANCEL RFQ
    # ========================================================================

    @staticmethod
    async def cancel_rfq(
        company_id: int,
        rfq_id: int,
        cancelled_by: int,
        reason: Optional[str] = None,
        bot: Optional[Any] = None,
    ) -> bool:
        """
        Cancel an RFQ. Allowed in draft or published states.

        The cancellation reason is recorded in audit_log (there is no
        status_reason column on rfqs). If bot is provided, invited
        suppliers are notified after commit.
        """
        cancelled_supplier_ids: List[int] = []

        async with get_session(company_id=company_id) as session:
            stmt = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt)).scalar_one_or_none()

            if not rfq or rfq.status not in [
                RfqStatus.draft,
                RfqStatus.published,
            ]:
                return False

            old_status = rfq.status.value
            rfq.status = RfqStatus.cancelled

            # Collect invited supplier ids for later notification
            stmt_sup = select(RfqSupplier).where(RfqSupplier.rfq_id == rfq_id)
            invited = (await session.execute(stmt_sup)).scalars().all()
            cancelled_supplier_ids = [s.supplier_id for s in invited]

            await audit_service.log_event(
                company_id=company_id,
                entity_table="rfqs",
                entity_id=rfq.id,
                action=AuditAction.STATUS_CHANGE,
                actor_user_id=cancelled_by,
                old_data={"status": old_status},
                new_data={"status": RfqStatus.cancelled.value},
                reason=reason or "RFQ cancelled",
                session=session,
            )

            await session.commit()

        logger.info(
            "Cancelled RFQ %s by user %s (reason=%s)",
            rfq_id,
            cancelled_by,
            reason,
        )

        # --- Notifications (after commit — best effort) ---
        if bot is not None and cancelled_supplier_ids:
            try:
                from app.services.notification_service import notification_service
                await notification_service.notify_rfq_cancelled(
                    bot=bot,
                    rfq_id=rfq_id,
                    company_id=company_id,
                    supplier_ids=cancelled_supplier_ids,
                    reason=reason,
                )
            except Exception:
                logger.exception(
                    "Failed to notify suppliers about RFQ %s cancellation",
                    rfq_id,
                )

        return True

    # ========================================================================
    # 7. READ
    # ========================================================================

    @staticmethod
    async def get_rfq_details(
        company_id: int,
        rfq_id: int,
    ) -> Optional[Dict]:
        """
        Fetch RFQ header, lines, and invited suppliers.
        """
        async with get_session(company_id=company_id) as session:
            stmt = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt)).scalar_one_or_none()
            if not rfq:
                return None

            stmt_lines = (
                select(RfqLine)
                .where(RfqLine.rfq_id == rfq_id)
                .order_by(RfqLine.line_no)
            )
            lines = (await session.execute(stmt_lines)).scalars().all()

            stmt_sups = select(RfqSupplier).where(
                RfqSupplier.rfq_id == rfq_id
            )
            suppliers = (await session.execute(stmt_sups)).scalars().all()

            return {
                "rfq": rfq,
                "lines": lines,
                "suppliers": suppliers,
            }

    @staticmethod
    async def list_company_rfqs(
        company_id: int,
        status: Optional[RfqStatus] = None,
        limit: int = 20,
    ) -> List[Rfq]:
        """List all RFQs for a company."""
        async with get_session(company_id=company_id) as session:
            stmt = select(Rfq)
            if status:
                stmt = stmt.where(Rfq.status == status)
            stmt = stmt.order_by(Rfq.created_at.desc()).limit(limit)

            result = await session.execute(stmt)
            return list(result.scalars().all())


# Singleton instance
rfq_service = RfqService()
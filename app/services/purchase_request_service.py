"""
Purchase Request Service - Business logic for managing purchase requests and their lines.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Create purchase requests with multiple line items.
- Manage the request lifecycle (draft -> submitted -> under_review -> approved/rejected/needs_revision).
- Enforce state machine transitions and business rules.
- All DB operations are async and respect RLS context (company_id).

KEY CHANGES from old version:
- Requests now have MULTIPLE line items (PurchaseRequestLine).
- Prices are NOT stored in the request (handled by pricing engine / quotations).
- 12-state lifecycle instead of 6.
- Unified decided_by / decided_at for approval/rejection/revision.
- decision_note is MANDATORY for rejected and needs_revision statuses.
- UPDATED: Allowed decisions on both 'submitted' and 'under_review' states to fix workflow gap.

IMPROVEMENTS (this revision):
- Uses get_logger(__name__) → correlation IDs.
- Logging uses %s formatting.
- Security-relevant rejections and state violations marked with
  extra={"security": True}.
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.purchase_request_line import PurchaseRequestLine
from app.database.models.enums import RequestStatus
from app.core.logger import get_logger

logger = get_logger(__name__)


class PurchaseRequestService:
    """
    Service class for Purchase Request operations.
    """

    # ================================================================
    # 1. CREATE REQUEST
    # ================================================================
    @staticmethod
    async def create_request(
        company_id: int,
        requester_id: int,
        title: str,
        lines: List[Dict[str, Any]],
    ) -> Optional[PurchaseRequest]:
        """
        Create a new purchase request with multiple line items.
        Starts in 'draft' status.

        Args:
            company_id: The company ID (for RLS isolation).
            requester_id: The user ID creating the request.
            title: Request title/summary.
            lines: List of dicts, each containing:
                   - item_name (str, required)
                   - item_id (int, optional - link to catalog)
                   - quantity (Decimal/float, required, > 0)
                   - uom_id (int, required)
                   - specs (str, optional)
                   - delivery_date (date, optional)
                   - delivery_place (str, optional)

        Returns:
            The created PurchaseRequest object, or None if failed.
        """
        if not lines:
            logger.warning(
                "Attempted to create request with no lines "
                "| company_id=%s | requester_id=%s",
                company_id,
                requester_id,
                extra={"security": True},
            )
            return None

        async with get_session(company_id=company_id) as session:
            # Create the header
            new_request = PurchaseRequest(
                company_id=company_id,
                requester_id=requester_id,
                title=title,
                status=RequestStatus.draft,
            )
            session.add(new_request)
            await session.flush()  # Get new_request.id

            # Create the lines
            for i, line_data in enumerate(lines, start=1):
                line = PurchaseRequestLine(
                    request_id=new_request.id,
                    line_no=i,
                    item_name=line_data["item_name"],
                    item_id=line_data.get("item_id"),
                    quantity=line_data["quantity"],
                    uom_id=line_data["uom_id"],
                    specs=line_data.get("specs"),
                    delivery_date=line_data.get("delivery_date"),
                    delivery_place=line_data.get("delivery_place"),
                )
                session.add(line)

            await session.commit()
            await session.refresh(new_request)

            logger.info(
                "Created purchase request | request_id=%s | lines=%s | company_id=%s | requester_id=%s",
                new_request.id,
                len(lines),
                company_id,
                requester_id,
            )
            return new_request

    # ================================================================
    # 2. FETCH REQUESTS
    # ================================================================
    @staticmethod
    async def get_request_by_id(request_id: int, company_id: int) -> Optional[PurchaseRequest]:
        """Fetch a specific request by ID (enforces RLS via company_id)."""
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    @staticmethod
    async def get_request_lines(request_id: int, company_id: int) -> List[PurchaseRequestLine]:
        """Fetch all line items for a specific request."""
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(PurchaseRequestLine)
                .where(PurchaseRequestLine.request_id == request_id)
                .order_by(PurchaseRequestLine.line_no.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    async def get_user_requests(
        company_id: int,
        user_id: int,
        status: Optional[RequestStatus] = None,
        limit: int = 20,
    ) -> List[PurchaseRequest]:
        """Get all requests created by a specific user."""
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(
                PurchaseRequest.requester_id == user_id
            )
            if status:
                stmt = stmt.where(PurchaseRequest.status == status)

            stmt = stmt.order_by(PurchaseRequest.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    async def get_pending_review_requests(
        company_id: int,
        limit: int = 20,
    ) -> List[PurchaseRequest]:
        """Get all requests waiting for manager review (status = submitted)."""
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(PurchaseRequest)
                .where(PurchaseRequest.status == RequestStatus.submitted)
                .order_by(PurchaseRequest.submitted_at.asc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ================================================================
    # 3. STATE TRANSITIONS (WORKFLOW)
    # ================================================================

    @staticmethod
    async def _transition_status(
        session: AsyncSession,
        request: PurchaseRequest,
        new_status: RequestStatus,
        decided_by: Optional[int] = None,
        decision_note: Optional[str] = None,
    ) -> None:
        """Internal helper to update status and decision fields."""
        request.status = new_status
        if decided_by is not None:
            request.decided_by = decided_by
            request.decided_at = datetime.now(timezone.utc)
            request.decision_note = decision_note

    @staticmethod
    async def submit_request(request_id: int, company_id: int) -> bool:
        """
        Transition: draft -> submitted.
        Sets submitted_at timestamp.
        """
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

            if not request or request.status != RequestStatus.draft:
                logger.warning(
                    "Cannot submit request | request_id=%s | reason=not_found_or_not_draft",
                    request_id,
                )
                return False

            request.status = RequestStatus.submitted
            request.submitted_at = datetime.now(timezone.utc)

            await session.commit()
            logger.info("Request submitted for review | request_id=%s", request_id)
            return True

    @staticmethod
    async def start_review(request_id: int, company_id: int, reviewer_id: int) -> bool:
        """Transition: submitted -> under_review."""
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

            if not request or request.status != RequestStatus.submitted:
                return False

            request.status = RequestStatus.under_review
            await session.commit()
            logger.info(
                "Request is now under review | request_id=%s | reviewer_id=%s",
                request_id,
                reviewer_id,
            )
            return True

    @staticmethod
    async def approve_request(request_id: int, company_id: int, approved_by: int) -> bool:
        """
        Transition: submitted/under_review -> approved.
        Allows approval directly from 'submitted' state.
        """
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

            if not request or request.status not in [
                RequestStatus.submitted,
                RequestStatus.under_review,
            ]:
                logger.warning(
                    "Cannot approve request | request_id=%s | state=%s",
                    request_id,
                    request.status.value if request else "None",
                )
                return False

            await PurchaseRequestService._transition_status(
                session, request, RequestStatus.approved, approved_by
            )
            await session.commit()
            logger.info(
                "Request approved | request_id=%s | by_user=%s",
                request_id,
                approved_by,
            )
            return True

    @staticmethod
    async def reject_request(
        request_id: int,
        company_id: int,
        rejected_by: int,
        reason: str,
    ) -> bool:
        """
        Transition: submitted/under_review -> rejected.
        REQUIRES a reason (decision_note). Enforced by DB CHECK constraint as well.
        """
        if not reason or not reason.strip():
            logger.warning(
                "Cannot reject request: reason is mandatory "
                "| request_id=%s | by_user=%s",
                request_id,
                rejected_by,
                extra={"security": True},
            )
            return False

        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

            if not request or request.status not in [
                RequestStatus.submitted,
                RequestStatus.under_review,
            ]:
                return False

            await PurchaseRequestService._transition_status(
                session, request, RequestStatus.rejected, rejected_by, reason.strip()
            )
            await session.commit()
            logger.info(
                "Request rejected | request_id=%s | by_user=%s",
                request_id,
                rejected_by,
            )
            return True

    @staticmethod
    async def request_revision(
        request_id: int,
        company_id: int,
        reviewer_id: int,
        notes: str,
    ) -> bool:
        """
        Transition: submitted/under_review -> needs_revision.
        REQUIRES notes (decision_note) explaining what needs to be changed.
        """
        if not notes or not notes.strip():
            logger.warning(
                "Cannot request revision: notes are mandatory "
                "| request_id=%s | by_user=%s",
                request_id,
                reviewer_id,
                extra={"security": True},
            )
            return False

        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

            if not request or request.status not in [
                RequestStatus.submitted,
                RequestStatus.under_review,
            ]:
                return False

            await PurchaseRequestService._transition_status(
                session,
                request,
                RequestStatus.needs_revision,
                reviewer_id,
                notes.strip(),
            )
            await session.commit()
            logger.info(
                "Request sent back for revision | request_id=%s | by_user=%s",
                request_id,
                reviewer_id,
            )
            return True

    @staticmethod
    async def resubmit_request(request_id: int, company_id: int) -> bool:
        """Transition: needs_revision -> submitted."""
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

            if not request or request.status != RequestStatus.needs_revision:
                return False

            request.status = RequestStatus.submitted
            request.submitted_at = datetime.now(timezone.utc)
            # Clear previous decision fields so it's fresh for the next review
            request.decided_by = None
            request.decided_at = None
            request.decision_note = None

            await session.commit()
            logger.info("Request resubmitted for review | request_id=%s", request_id)
            return True

    @staticmethod
    async def cancel_request(request_id: int, company_id: int, cancelled_by: int) -> bool:
        """
        Transition: draft/submitted/under_review/needs_revision/approved -> cancelled.
        (Cannot cancel if RFQ is already created or in later stages).
        """
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(PurchaseRequest.id == request_id)
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

            if not request:
                return False

            allowed_states = {
                RequestStatus.draft,
                RequestStatus.submitted,
                RequestStatus.under_review,
                RequestStatus.needs_revision,
                RequestStatus.approved,
            }
            if request.status not in allowed_states:
                logger.warning(
                    "Cannot cancel request from current state "
                    "| request_id=%s | state=%s",
                    request_id,
                    request.status.value,
                )
                return False

            await PurchaseRequestService._transition_status(
                session, request, RequestStatus.cancelled, cancelled_by
            )
            await session.commit()
            logger.info(
                "Request cancelled | request_id=%s | by_user=%s",
                request_id,
                cancelled_by,
            )
            return True


# Singleton instance
purchase_request_service = PurchaseRequestService()
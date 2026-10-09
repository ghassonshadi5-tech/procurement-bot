"""
Quotation Service - Business logic for managing supplier quotations (offers).
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Create draft quotations for open/targeted RFQs.
- Enforce deadline rules (reject late submissions).
- Create immutable snapshots (quotation_snapshots) upon submission to preserve historical pricing.
- Enforce state machine transitions (draft -> submitted / late / withdrawn).
- All DB operations are async and respect RLS context.

Key Rules from Lifecycle Document:
- Supplier can only submit if RFQ is published and deadline hasn't passed.
- Server time is the official reference for deadline comparison.
- Late submissions are rejected and logged in audit_log.
- Historical prices are frozen in quotation_snapshots upon submission.

CHANGE LOG:
- Made currency a parameter in create_draft_quotation (was hardcoded "USD").
- Added unit_price > 0 validation in add_or_update_line.
- Used InviteStatus.responded.value instead of "responded" string.
- Added AuditLog entry on late submission rejection.
- (Deferred) price_type / price_basis still hardcoded — needs pricing_service integration.
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.rfq import Rfq
from app.database.models.rfq_line import RfqLine
from app.database.models.rfq_supplier import RfqSupplier
from app.database.models.quotation import Quotation
from app.database.models.quotation_line import QuotationLine
from app.database.models.quotation_snapshot import QuotationSnapshot
from app.database.models.audit_log import AuditLog
from app.database.models.enums import (
    RfqStatus,
    QuotationStatus,
    PriceType,
    InviteStatus,
    AuditAction,
)
from app.core.logger import get_logger

logger = get_logger(__name__)


class QuotationService:
    """
    Service class for Quotation (Offer) operations.
    """

    # ========================================================================
    # 1. CREATE DRAFT QUOTATION
    # ========================================================================
    @staticmethod
    async def create_draft_quotation(
        rfq_id: int,
        supplier_id: int,
        company_id: int,
        created_by: int,
        currency: str = "USD",
    ) -> Optional[Quotation]:
        """
        Create a new draft quotation for a supplier responding to an RFQ.
        Validates that the RFQ is published and the supplier is eligible.

        Args:
            rfq_id: The RFQ being responded to.
            supplier_id: The supplier creating the quotation.
            company_id: The buyer company (RLS scope).
            created_by: User ID who created the quotation.
            currency: ISO 4217 currency code (default: "USD").
        """
        async with get_session(
            company_id=company_id, is_supplier_owner=True
        ) as session:
            # 1. Fetch RFQ and validate state
            stmt_rfq = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt_rfq)).scalar_one_or_none()

            if not rfq:
                logger.warning("RFQ not found | rfq_id=%s", rfq_id)
                return None

            if rfq.status != RfqStatus.published:
                logger.warning(
                    "Cannot create quotation: RFQ is not published "
                    "| rfq_id=%s | status=%s",
                    rfq_id,
                    rfq.status.value if hasattr(rfq.status, "value") else rfq.status,
                )
                return None

            # 2. Check if supplier is invited (for targeted/mixed) or eligible
            stmt_inv = select(RfqSupplier).where(
                RfqSupplier.rfq_id == rfq_id,
                RfqSupplier.supplier_id == supplier_id,
            )
            invitation = (await session.execute(stmt_inv)).scalar_one_or_none()

            rfq_method = (
                rfq.method.value if hasattr(rfq.method, "value") else rfq.method
            )
            if not invitation and rfq_method == "targeted":
                logger.warning(
                    "Supplier not invited to targeted RFQ "
                    "| supplier_id=%s | rfq_id=%s",
                    supplier_id,
                    rfq_id,
                )
                return None

            # 3. Check if supplier already has a quotation for this RFQ (UNIQUE constraint)
            stmt_exists = select(Quotation).where(
                Quotation.rfq_id == rfq_id,
                Quotation.supplier_id == supplier_id,
            )
            existing = (await session.execute(stmt_exists)).scalar_one_or_none()
            if existing:
                logger.info(
                    "Supplier already has quotation for this RFQ "
                    "| supplier_id=%s | quotation_id=%s | rfq_id=%s",
                    supplier_id,
                    existing.id,
                    rfq_id,
                )
                return existing

            # 4. Create Draft Quotation
            new_quotation = Quotation(
                rfq_id=rfq_id,
                supplier_id=supplier_id,
                company_id=company_id,
                status=QuotationStatus.draft,
                currency=currency,
                created_by=created_by,
            )
            session.add(new_quotation)
            await session.commit()
            await session.refresh(new_quotation)

            logger.info(
                "Created draft quotation | quotation_id=%s | rfq_id=%s | supplier_id=%s",
                new_quotation.id,
                rfq_id,
                supplier_id,
            )
            return new_quotation

    # ========================================================================
    # 2. ADD / UPDATE LINE
    # ========================================================================
    @staticmethod
    async def add_or_update_line(
        quotation_id: int,
        rfq_line_id: int,
        supplier_id: int,
        company_id: int,
        unit_price: Decimal,
        quantity_available: Decimal,
        currency: str = "USD",
        delivery_date: Optional[Any] = None,
        line_terms: Optional[str] = None,
    ) -> Optional[QuotationLine]:
        """
        Add or update a line item in a DRAFT quotation.
        Prevents modification if the quotation is already submitted.

        Validates:
        - unit_price > 0
        - quantity_available > 0
        """
        # Validate inputs
        if unit_price is None or unit_price <= 0:
            logger.warning(
                "Invalid unit_price | quotation_id=%s | unit_price=%s",
                quotation_id,
                unit_price,
            )
            return None

        if quantity_available is None or quantity_available <= 0:
            logger.warning(
                "Invalid quantity_available | quotation_id=%s | qty=%s",
                quotation_id,
                quantity_available,
            )
            return None

        async with get_session(
            company_id=company_id, is_supplier_owner=True
        ) as session:
            # Fetch quotation and ensure it's in DRAFT state
            stmt_q = select(Quotation).where(
                Quotation.id == quotation_id,
                Quotation.supplier_id == supplier_id,
            )
            quotation = (await session.execute(stmt_q)).scalar_one_or_none()

            if not quotation:
                return None

            if quotation.status != QuotationStatus.draft:
                logger.warning(
                    "Cannot modify quotation: not in draft state "
                    "| quotation_id=%s | status=%s",
                    quotation_id,
                    quotation.status.value
                    if hasattr(quotation.status, "value")
                    else quotation.status,
                )
                return None

            # Upsert the line (Update if exists, Insert if not)
            stmt_line = select(QuotationLine).where(
                QuotationLine.quotation_id == quotation_id,
                QuotationLine.rfq_line_id == rfq_line_id,
            )
            line = (await session.execute(stmt_line)).scalar_one_or_none()

            if line:
                line.unit_price = unit_price
                line.quantity_available = quantity_available
                line.currency = currency
                line.delivery_date = delivery_date
                line.line_terms = line_terms
            else:
                # Fetch RFQ line to get line_no
                stmt_rfq_line = select(RfqLine).where(RfqLine.id == rfq_line_id)
                rfq_line = (await session.execute(stmt_rfq_line)).scalar_one_or_none()
                if not rfq_line:
                    logger.warning(
                        "RFQ line not found | rfq_line_id=%s", rfq_line_id
                    )
                    return None

                line = QuotationLine(
                    quotation_id=quotation_id,
                    rfq_line_id=rfq_line_id,
                    line_no=rfq_line.line_no,
                    unit_price=unit_price,
                    quantity_available=quantity_available,
                    currency=currency,
                    delivery_date=delivery_date,
                    line_terms=line_terms,
                )
                session.add(line)

            await session.commit()
            await session.refresh(line)
            return line

    # ========================================================================
    # 3. SUBMIT QUOTATION
    # ========================================================================
    @staticmethod
    async def submit_quotation(
        quotation_id: int,
        supplier_id: int,
        company_id: int,
    ) -> Dict[str, Any]:
        """
        Submit the quotation.

        1. Checks deadline (server time is reference).
        2. If late → marks as 'late', rejects, logs AuditLog.
        3. If on time → creates immutable snapshots, marks as 'submitted'.
        """
        async with get_session(
            company_id=company_id, is_supplier_owner=True
        ) as session:
            # 1. Fetch Quotation and RFQ
            stmt_q = select(Quotation).where(
                Quotation.id == quotation_id,
                Quotation.supplier_id == supplier_id,
            )
            quotation = (await session.execute(stmt_q)).scalar_one_or_none()
            if not quotation or quotation.status != QuotationStatus.draft:
                return {
                    "success": False,
                    "error": "العرض غير موجود أو تم تقديمه بالفعل.",
                }

            stmt_rfq = select(Rfq).where(Rfq.id == quotation.rfq_id)
            rfq = (await session.execute(stmt_rfq)).scalar_one_or_none()
            if not rfq:
                return {"success": False, "error": "المنافسة غير موجودة."}

            now = datetime.now(timezone.utc)

            # 2. Check Deadline
            if rfq.deadline and now > rfq.deadline:
                quotation.status = QuotationStatus.late
                quotation.is_late = True
                quotation.submitted_at = now

                # AuditLog entry for late submission
                audit = AuditLog(
                    entity_table="quotations",
                    entity_id=quotation.id,
                    action=AuditAction.REJECT,
                    actor_user_id=quotation.created_by,
                    company_id=company_id,
                    reason="Late submission: deadline passed",
                    new_data={
                        "status": QuotationStatus.late.value,
                        "is_late": True,
                        "submitted_at": now.isoformat(),
                    },
                )
                session.add(audit)

                await session.commit()

                logger.warning(
                    "LATE_SUBMISSION | quotation_id=%s | supplier_id=%s | rfq_id=%s",
                    quotation_id,
                    supplier_id,
                    rfq.id,
                    extra={"security": True},
                )
                return {
                    "success": False,
                    "error": "انتهت مهلة المنافسة. تم رفض العرض كتقديم متأخر.",
                }

            # 3. Fetch all quotation lines
            stmt_lines = select(QuotationLine).where(
                QuotationLine.quotation_id == quotation_id
            )
            lines = (await session.execute(stmt_lines)).scalars().all()

            if not lines:
                return {
                    "success": False,
                    "error": "لا يمكن تقديم عرض فارغ. أضف بنداً واحداً على الأقل.",
                }

            # 4. Create Immutable Snapshots for each line
            for line in lines:
                snapshot = QuotationSnapshot(
                    quotation_id=quotation_id,
                    quotation_line_id=line.id,
                    price_type=PriceType.custom,
                    currency=line.currency,
                    unit_price=line.unit_price,
                    quantity=line.quantity_available,
                    delivery_cost=quotation.delivery_cost,
                    extra_fees=quotation.extra_fees,
                    lead_time_days=quotation.lead_time_days,
                    price_basis="manual",
                    frozen_payload={
                        "unit_price": str(line.unit_price),
                        "currency": line.currency,
                        "delivery_date": str(line.delivery_date)
                        if line.delivery_date
                        else None,
                        "line_terms": line.line_terms,
                    },
                    frozen_by=quotation.created_by,
                )
                session.add(snapshot)

            # 5. Update Quotation Status
            quotation.status = QuotationStatus.submitted
            quotation.submitted_at = now

            # Update RFQ Supplier status
            stmt_inv = select(RfqSupplier).where(
                RfqSupplier.rfq_id == rfq.id,
                RfqSupplier.supplier_id == supplier_id,
            )
            inv = (await session.execute(stmt_inv)).scalar_one_or_none()
            if inv:
                inv.invitation_status = InviteStatus.responded.value
                inv.responded_at = now

            await session.commit()
            logger.info(
                "Quotation submitted | quotation_id=%s | lines=%s | rfq_id=%s",
                quotation_id,
                len(lines),
                rfq.id,
            )
            return {"success": True, "quotation_id": quotation_id}

    # ========================================================================
    # 4. WITHDRAW QUOTATION
    # ========================================================================
    @staticmethod
    async def withdraw_quotation(
        quotation_id: int,
        supplier_id: int,
        company_id: int,
    ) -> bool:
        """
        Withdraw a submitted quotation (allowed only if RFQ is still published).
        """
        async with get_session(
            company_id=company_id, is_supplier_owner=True
        ) as session:
            stmt_q = select(Quotation).where(
                Quotation.id == quotation_id,
                Quotation.supplier_id == supplier_id,
            )
            quotation = (await session.execute(stmt_q)).scalar_one_or_none()

            if not quotation or quotation.status != QuotationStatus.submitted:
                return False

            stmt_rfq = select(Rfq).where(Rfq.id == quotation.rfq_id)
            rfq = (await session.execute(stmt_rfq)).scalar_one_or_none()
            if not rfq:
                return False

            # Can only withdraw if RFQ is still published
            if rfq.status != RfqStatus.published:
                logger.warning(
                    "Cannot withdraw quotation: RFQ is not published "
                    "| quotation_id=%s | rfq_id=%s | status=%s",
                    quotation_id,
                    rfq.id,
                    rfq.status.value if hasattr(rfq.status, "value") else rfq.status,
                )
                return False

            quotation.status = QuotationStatus.withdrawn
            await session.commit()
            logger.info(
                "Quotation withdrawn | quotation_id=%s | supplier_id=%s",
                quotation_id,
                supplier_id,
            )
            return True

    # ========================================================================
    # 5. READ QUOTATIONS
    # ========================================================================
    @staticmethod
    async def get_rfq_quotations(
        rfq_id: int,
        company_id: int,
    ) -> List[Quotation]:
        """
        Fetch all valid quotations for an RFQ (for the buyer's comparison view).
        Excludes 'late' and 'withdrawn' quotations from the main comparison.
        """
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(Quotation)
                .where(
                    Quotation.rfq_id == rfq_id,
                    Quotation.status.in_(
                        [
                            QuotationStatus.submitted,
                            QuotationStatus.accepted,
                            QuotationStatus.rejected,
                            QuotationStatus.not_selected,
                        ]
                    ),
                )
                .order_by(Quotation.submitted_at.asc())
            )

            result = await session.execute(stmt)
            return list(result.scalars().all())


# Singleton instance
quotation_service = QuotationService()
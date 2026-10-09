"""
Goods Receipt Service - Business logic for receiving goods and recording variances.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Create goods receipt from an awarded quotation.
- Add receipt lines with expected and received quantities.
- Enforce mandatory variance reasons when quantities differ.
- Confirm receipt and transition purchase request to 'received' status.
- All DB operations are async and respect RLS context (company_id).

Key Rules:
- Only ONE receipt per quotation (UNIQUE constraint).
- Variance reason is mandatory when quantity_received != quantity_expected.
- variance_qty is auto-calculated by the database (GENERATED column).
- Only Storekeeper (and authorized users) can create/confirm receipts.
- Receipt can only be created from an 'accepted' quotation of an 'awarded' RFQ.

CHANGE LOG:
- Added AuditLog entry (action=STATUS_CHANGE) on receipt confirmation.
- total_expected/total_received sums now use Decimal("0") as start value.
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.goods_receipt import GoodsReceipt
from app.database.models.goods_receipt_line import GoodsReceiptLine
from app.database.models.quotation import Quotation
from app.database.models.quotation_line import QuotationLine
from app.database.models.rfq import Rfq
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.audit_log import AuditLog
from app.database.models.enums import (
    QuotationStatus,
    RfqStatus,
    RequestStatus,
    AuditAction,
)
from app.core.logger import get_logger

logger = get_logger(__name__)


class GoodsReceiptService:
    """
    Service class for Goods Receipt operations.
    """

    # ========================================================================
    # 1. CREATE GOODS RECEIPT
    # ========================================================================

    @staticmethod
    async def create_goods_receipt(
        company_id: int,
        quotation_id: int,
        received_by: int,
    ) -> Dict[str, Any]:
        """
        Create a new goods receipt for an awarded quotation.

        Validates:
        - Quotation exists and is in 'accepted' status.
        - RFQ is in 'awarded' status.
        - No existing receipt for this quotation (UNIQUE constraint).

        Returns:
            {"success": True, "receipt": GoodsReceipt} or {"success": False, "error": str}
        """
        async with get_session(company_id=company_id) as session:
            # 1. Fetch quotation and validate
            stmt_q = select(Quotation).where(
                Quotation.id == quotation_id,
                Quotation.company_id == company_id,
            )
            quotation = (await session.execute(stmt_q)).scalar_one_or_none()

            if not quotation:
                return {"success": False, "error": "العرض غير موجود."}

            if quotation.status != QuotationStatus.accepted:
                current_status = (
                    quotation.status.value
                    if hasattr(quotation.status, "value")
                    else quotation.status
                )
                return {
                    "success": False,
                    "error": (
                        f"لا يمكن إنشاء استلام لعرض في حالة ({current_status}). "
                        f"يجب أن يكون العرض مقبولاً."
                    ),
                }

            # 2. Fetch RFQ and validate
            stmt_rfq = select(Rfq).where(Rfq.id == quotation.rfq_id)
            rfq = (await session.execute(stmt_rfq)).scalar_one_or_none()

            if not rfq or rfq.status != RfqStatus.awarded:
                return {
                    "success": False,
                    "error": (
                        "طلب العروض ليس في حالة 'تمت الترسية'. "
                        "لا يمكن الاستلام."
                    ),
                }

            # 3. Check if receipt already exists
            stmt_existing = select(GoodsReceipt).where(
                GoodsReceipt.quotation_id == quotation_id
            )
            existing = (await session.execute(stmt_existing)).scalar_one_or_none()
            if existing:
                return {
                    "success": False,
                    "error": (
                        f"يوجد استلام مسبق لهذا العرض برقم {existing.id}."
                    ),
                }

            # 4. Create goods receipt
            receipt = GoodsReceipt(
                company_id=company_id,
                rfq_id=rfq.id,
                quotation_id=quotation_id,
                received_by=received_by,
            )
            session.add(receipt)
            await session.commit()
            await session.refresh(receipt)

            logger.info(
                "Created goods receipt | receipt_id=%s | quotation_id=%s",
                receipt.id,
                quotation_id,
            )
            return {"success": True, "receipt": receipt}

    # ========================================================================
    # 2. ADD / UPDATE RECEIPT LINES
    # ========================================================================

    @staticmethod
    async def add_receipt_line(
        company_id: int,
        receipt_id: int,
        quotation_line_id: int,
        quantity_expected: Decimal,
        quantity_received: Decimal,
        variance_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Add a line to the goods receipt.

        Validates:
        - quantity_expected > 0
        - quantity_received >= 0
        - If quantity_received != quantity_expected, variance_reason is MANDATORY.

        NOTE: variance_qty is auto-calculated by the database as:
              quantity_received - quantity_expected (GENERATED column)
        """
        async with get_session(company_id=company_id) as session:
            # 1. Fetch receipt and validate
            stmt_r = select(GoodsReceipt).where(
                GoodsReceipt.id == receipt_id,
                GoodsReceipt.company_id == company_id,
            )
            receipt = (await session.execute(stmt_r)).scalar_one_or_none()

            if not receipt:
                return {"success": False, "error": "الاستلام غير موجود."}

            # 2. Fetch quotation line and validate
            stmt_ql = select(QuotationLine).where(
                QuotationLine.id == quotation_line_id
            )
            q_line = (await session.execute(stmt_ql)).scalar_one_or_none()

            if not q_line or q_line.quotation_id != receipt.quotation_id:
                return {
                    "success": False,
                    "error": "بند العرض غير موجود في هذا الاستلام.",
                }

            # 3. Validate quantities
            if quantity_expected <= 0:
                return {
                    "success": False,
                    "error": "الكمية المتوقعة يجب أن تكون أكبر من صفر.",
                }

            if quantity_received < 0:
                return {
                    "success": False,
                    "error": "الكمية المستلمة لا يمكن أن تكون سالبة.",
                }

            # 4. Validate variance reason (MANDATORY if there's a difference)
            has_variance = quantity_received != quantity_expected
            if has_variance and (
                not variance_reason or not variance_reason.strip()
            ):
                return {
                    "success": False,
                    "error": (
                        "سبب الفرق إلزامي عند وجود فرق بين "
                        "الكمية المتوقعة والمستلمة."
                    ),
                }

            # 5. Upsert the line
            stmt_existing_line = select(GoodsReceiptLine).where(
                GoodsReceiptLine.receipt_id == receipt_id,
                GoodsReceiptLine.quotation_line_id == quotation_line_id,
            )
            existing_line = (
                await session.execute(stmt_existing_line)
            ).scalar_one_or_none()

            if existing_line:
                existing_line.quantity_expected = quantity_expected
                existing_line.quantity_received = quantity_received
                existing_line.variance_reason = (
                    variance_reason if has_variance else None
                )
                line = existing_line
            else:
                line = GoodsReceiptLine(
                    receipt_id=receipt_id,
                    quotation_line_id=quotation_line_id,
                    quantity_expected=quantity_expected,
                    quantity_received=quantity_received,
                    variance_reason=variance_reason if has_variance else None,
                )
                session.add(line)

            await session.commit()
            await session.refresh(line)

            variance = quantity_received - quantity_expected
            logger.info(
                "Added receipt line | receipt_id=%s | expected=%s | received=%s | variance=%s",
                receipt_id,
                quantity_expected,
                quantity_received,
                variance,
            )

            return {"success": True, "line": line, "variance": variance}

    # ========================================================================
    # 3. CONFIRM GOODS RECEIPT (Finalize)
    # ========================================================================

    @staticmethod
    async def confirm_goods_receipt(
        company_id: int,
        receipt_id: int,
        confirmed_by: int,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Confirm (finalize) the goods receipt.

        This operation:
        1. Validates that all quotation lines have been received.
        2. Validates that all variances have reasons.
        3. Updates the receipt note (if provided).
        4. Transitions the purchase request from 'awarded' to 'received'.
        5. Logs the confirmation (logger + AuditLog).

        All changes happen in a single transaction.
        """
        async with get_session(company_id=company_id) as session:
            # 1. Fetch receipt
            stmt_r = select(GoodsReceipt).where(
                GoodsReceipt.id == receipt_id,
                GoodsReceipt.company_id == company_id,
            )
            receipt = (await session.execute(stmt_r)).scalar_one_or_none()

            if not receipt:
                return {"success": False, "error": "الاستلام غير موجود."}

            # 2. Fetch all quotation lines for this receipt's quotation
            stmt_q_lines = select(QuotationLine).where(
                QuotationLine.quotation_id == receipt.quotation_id
            )
            quotation_lines = (
                await session.execute(stmt_q_lines)
            ).scalars().all()

            # 3. Fetch all receipt lines
            stmt_r_lines = select(GoodsReceiptLine).where(
                GoodsReceiptLine.receipt_id == receipt_id
            )
            receipt_lines = (
                await session.execute(stmt_r_lines)
            ).scalars().all()

            receipt_lines_map = {rl.quotation_line_id: rl for rl in receipt_lines}

            # 4. Validate completeness
            missing_lines = []
            for ql in quotation_lines:
                if ql.id not in receipt_lines_map:
                    missing_lines.append(ql.line_no)

            if missing_lines:
                missing_str = ", ".join(map(str, missing_lines))
                return {
                    "success": False,
                    "error": (
                        f"لا يمكن التأكيد: توجد بنود لم يتم استلامها بعد "
                        f"(أرقام البنود: {missing_str})."
                    ),
                }

            # 5. Validate variance reasons
            for rl in receipt_lines:
                has_variance = rl.quantity_received != rl.quantity_expected
                if has_variance and (
                    not rl.variance_reason or not rl.variance_reason.strip()
                ):
                    return {
                        "success": False,
                        "error": (
                            "لا يمكن التأكيد: يوجد فرق في الكمية "
                            "دون سبب مسجل."
                        ),
                    }

            # 6. Update receipt note if provided
            if note:
                receipt.note = note

            # 7. Fetch RFQ to get the request_id
            stmt_rfq = select(Rfq).where(Rfq.id == receipt.rfq_id)
            rfq = (await session.execute(stmt_rfq)).scalar_one_or_none()

            if not rfq:
                return {"success": False, "error": "طلب العروض المرتبط غير موجود."}

            # 8. Update purchase request status to 'received'
            stmt_request = select(PurchaseRequest).where(
                PurchaseRequest.id == rfq.request_id
            )
            request = (await session.execute(stmt_request)).scalar_one_or_none()

            if not request:
                return {"success": False, "error": "طلب الشراء المرتبط غير موجود."}

            if request.status != RequestStatus.awarded:
                current_status = (
                    request.status.value
                    if hasattr(request.status, "value")
                    else request.status
                )
                return {
                    "success": False,
                    "error": (
                        f"طلب الشراء في حالة ({current_status}) "
                        f"ولا يمكن نقله إلى 'مستلم'."
                    ),
                }

            request.status = RequestStatus.received

            # Calculate summary statistics (before commit)
            total_expected = sum(
                (rl.quantity_expected for rl in receipt_lines), Decimal("0")
            )
            total_received = sum(
                (rl.quantity_received for rl in receipt_lines), Decimal("0")
            )
            total_variance = total_received - total_expected

            # 9. AuditLog for the confirmation
            audit = AuditLog(
                entity_table="goods_receipts",
                entity_id=receipt_id,
                action=AuditAction.STATUS_CHANGE,
                actor_user_id=confirmed_by,
                company_id=company_id,
                reason=note,
                new_data={
                    "status": "confirmed",
                    "total_expected": str(total_expected),
                    "total_received": str(total_received),
                    "total_variance": str(total_variance),
                    "lines_count": len(receipt_lines),
                },
            )
            session.add(audit)

            await session.commit()

            logger.info(
                "Confirmed goods receipt | receipt_id=%s | lines=%s | total_expected=%s | total_received=%s | total_variance=%s | confirmed_by=%s",
                receipt_id,
                len(receipt_lines),
                total_expected,
                total_received,
                total_variance,
                confirmed_by,
                extra={"security": True},
            )

            return {
                "success": True,
                "receipt_id": receipt_id,
                "total_expected": total_expected,
                "total_received": total_received,
                "total_variance": total_variance,
                "lines_count": len(receipt_lines),
            }

    # ========================================================================
    # 4. GET RECEIPT DETAILS
    # ========================================================================

    @staticmethod
    async def get_receipt_details(
        company_id: int,
        receipt_id: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Get full details of a goods receipt including all lines and variances.
        """
        async with get_session(company_id=company_id) as session:
            # Fetch receipt
            stmt_r = select(GoodsReceipt).where(
                GoodsReceipt.id == receipt_id,
                GoodsReceipt.company_id == company_id,
            )
            receipt = (await session.execute(stmt_r)).scalar_one_or_none()

            if not receipt:
                return None

            # Fetch receipt lines with quotation line details
            stmt_lines = (
                select(GoodsReceiptLine, QuotationLine)
                .join(
                    QuotationLine,
                    GoodsReceiptLine.quotation_line_id == QuotationLine.id,
                )
                .where(GoodsReceiptLine.receipt_id == receipt_id)
                .order_by(QuotationLine.line_no.asc())
            )
            lines_result = await session.execute(stmt_lines)
            lines_with_details = lines_result.all()

            lines_data = []
            for gr_line, q_line in lines_with_details:
                variance = gr_line.quantity_received - gr_line.quantity_expected
                lines_data.append(
                    {
                        "line_no": q_line.line_no,
                        "unit_price": q_line.unit_price,
                        "currency": q_line.currency,
                        "quantity_expected": gr_line.quantity_expected,
                        "quantity_received": gr_line.quantity_received,
                        "variance_qty": variance,
                        "variance_reason": gr_line.variance_reason,
                        "has_variance": variance != 0,
                    }
                )

            return {
                "receipt": receipt,
                "lines": lines_data,
                "total_lines": len(lines_data),
            }

    # ========================================================================
    # 5. LIST RECEIPTS FOR COMPANY
    # ========================================================================

    @staticmethod
    async def list_company_receipts(
        company_id: int,
        limit: int = 20,
    ) -> List[GoodsReceipt]:
        """List all goods receipts for a company."""
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(GoodsReceipt)
                .where(GoodsReceipt.company_id == company_id)
                .order_by(GoodsReceipt.received_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())


# Singleton instance
goods_receipt_service = GoodsReceiptService()
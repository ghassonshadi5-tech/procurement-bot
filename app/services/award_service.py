"""
Award Service - Business logic for quotation comparison and award decisions.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Provide unified comparison data for submitted quotations in a closed RFQ.
- Record the human decision to award an RFQ to a specific quotation.
- Enforce state machine transitions (RFQ: closed -> awarded, Request: rfq_created -> awarded).
- Mark winning quotation as 'accepted' and others as 'not_selected'.

Key Rules from Lifecycle Document:
- Selection is ALWAYS a human decision from an authorized user.
- The system NEVER auto-selects the lowest price or any quotation.
- Comparison must be on a comparable basis (currency, quantity, delivery, fees, terms).
- If quotations are incomplete or incomparable, the system does NOT invent values.
- Award decision is final (one per RFQ, enforced by UNIQUE constraint).
- Reason is mandatory when reason_required policy is enabled.

CHANGE LOG:
- get_comparison_data now includes accepted/not_selected/rejected quotations
  (so it still works after award).
- Added AuditLog entry (action=AWARD) on successful award.
- Added AuditLog entries for each not_selected quotation.
- Saved supplier_id before commit to avoid lazy-load issues.
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.rfq import Rfq
from app.database.models.rfq_line import RfqLine
from app.database.models.quotation import Quotation
from app.database.models.quotation_line import QuotationLine
from app.database.models.quotation_snapshot import QuotationSnapshot
from app.database.models.award_decision import AwardDecision
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.audit_log import AuditLog
from app.database.models.enums import (
    RfqStatus,
    QuotationStatus,
    RequestStatus,
    AuditAction,
)
from app.core.logger import get_logger

logger = get_logger(__name__)


class AwardService:
    """
    Service class for quotation comparison and award decisions.
    """

    # ========================================================================
    # 1. COMPARISON DATA (Read-only, for display to authorized users)
    # ========================================================================

    @staticmethod
    async def get_comparison_data(
        rfq_id: int,
        company_id: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Get unified comparison data for all quotations in a closed RFQ.

        Returns a normalized structure that allows the buyer to compare quotations
        on a comparable basis: currency, unit prices, delivery costs, fees, and totals.

        NOTE: This function does NOT rank or select quotations. It only provides
        the data for the human decision-maker to evaluate.
        """
        async with get_session(company_id=company_id) as session:
            # 1. Fetch RFQ and validate state
            stmt_rfq = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt_rfq)).scalar_one_or_none()

            if not rfq:
                return None

            # Comparison is only available after RFQ is closed
            if rfq.status not in [RfqStatus.closed, RfqStatus.awarded]:
                logger.warning(
                    "Cannot compare quotations: RFQ is not closed "
                    "| rfq_id=%s | status=%s",
                    rfq_id,
                    rfq.status.value if hasattr(rfq.status, "value") else rfq.status,
                )
                return None

            # 2. Fetch all relevant quotations (submitted + terminal states)
            # Including accepted/not_selected/rejected so that the comparison
            # remains viewable after the award decision.
            stmt_quotations = (
                select(Quotation)
                .where(
                    Quotation.rfq_id == rfq_id,
                    Quotation.status.in_(
                        [
                            QuotationStatus.submitted,
                            QuotationStatus.accepted,
                            QuotationStatus.not_selected,
                            QuotationStatus.rejected,
                        ]
                    ),
                )
                .order_by(Quotation.submitted_at.asc())
            )

            quotations = (await session.execute(stmt_quotations)).scalars().all()

            if not quotations:
                return {
                    "rfq": rfq,
                    "quotations": [],
                    "warning": (
                        "لا توجد عروض صالحة للمقارنة. "
                        "لم يقدم أي مورد عرضاً قبل انتهاء المهلة."
                    ),
                }

            # 3. Build comparison data for each quotation
            comparison_data = []
            currencies_in_use = set()

            for quotation in quotations:
                # Fetch quotation lines with their snapshots
                stmt_lines = (
                    select(QuotationLine, QuotationSnapshot)
                    .outerjoin(
                        QuotationSnapshot,
                        QuotationSnapshot.quotation_line_id == QuotationLine.id,
                    )
                    .where(QuotationLine.quotation_id == quotation.id)
                    .order_by(QuotationLine.line_no.asc())
                )
                lines_result = await session.execute(stmt_lines)
                lines_with_snapshots = lines_result.all()

                # Calculate total cost for this quotation
                lines_data = []
                subtotal = Decimal("0")

                for line, snapshot in lines_with_snapshots:
                    line_total = line.unit_price * line.quantity_available
                    subtotal += line_total

                    lines_data.append(
                        {
                            "line_no": line.line_no,
                            "unit_price": line.unit_price,
                            "quantity_available": line.quantity_available,
                            "currency": line.currency,
                            "line_total": line_total,
                            "delivery_date": line.delivery_date,
                            "line_terms": line.line_terms,
                            "price_basis": snapshot.price_basis
                            if snapshot
                            else None,
                            "frozen_at": snapshot.frozen_at if snapshot else None,
                        }
                    )
                    currencies_in_use.add(line.currency)

                # Add header-level costs
                delivery_cost = quotation.delivery_cost or Decimal("0")
                extra_fees = quotation.extra_fees or Decimal("0")
                total_cost = subtotal + delivery_cost + extra_fees

                quotation_summary = {
                    "quotation_id": quotation.id,
                    "supplier_id": quotation.supplier_id,
                    "status": quotation.status,
                    "submitted_at": quotation.submitted_at,
                    "is_late": quotation.is_late,
                    "currency": quotation.currency,
                    "delivery_terms": quotation.delivery_terms,
                    "delivery_cost": delivery_cost,
                    "extra_fees": extra_fees,
                    "lead_time_days": quotation.lead_time_days,
                    "valid_until": quotation.valid_until,
                    "notes": quotation.notes,
                    "lines": lines_data,
                    "subtotal": subtotal,
                    "total_cost": total_cost,
                }
                comparison_data.append(quotation_summary)

            # 4. Check comparability (currency consistency)
            comparability_warning = None
            if len(currencies_in_use) > 1:
                comparability_warning = (
                    f"⚠️ تنبيه: العروض مقدمة بعملات مختلفة "
                    f"({', '.join(sorted(currencies_in_use))}). "
                    f"لا يمكن المقارنة المباشرة بينها دون توحيد العملة."
                )
                logger.warning(
                    "Comparability warning: multiple currencies in use "
                    "| rfq_id=%s | currencies=%s",
                    rfq_id,
                    sorted(currencies_in_use),
                    extra={"security": True},
                )

            return {
                "rfq": rfq,
                "quotations": comparison_data,
                "total_quotations": len(comparison_data),
                "currencies_in_use": list(currencies_in_use),
                "comparability_warning": comparability_warning,
            }

    # ========================================================================
    # 2. AWARD DECISION (Human decision, recorded and enforced)
    # ========================================================================

    @staticmethod
    async def select_winning_quotation(
        rfq_id: int,
        quotation_id: int,
        company_id: int,
        selected_by: int,
        reason: Optional[str] = None,
        reason_required: bool = False,
    ) -> Dict[str, Any]:
        """
        Record the human decision to award an RFQ to a specific quotation.

        This is a transactional operation that:
        1. Validates RFQ state (must be 'closed')
        2. Validates quotation state (must be 'submitted')
        3. Validates reason (mandatory if reason_required is True)
        4. Creates the award decision record (UNIQUE constraint prevents duplicates)
        5. Updates winning quotation to 'accepted'
        6. Updates other quotations to 'not_selected'
        7. Updates RFQ status to 'awarded'
        8. Updates Purchase Request status to 'awarded'
        9. Logs AWARD to audit_log + not_selected entries

        All changes happen in a single transaction to ensure consistency.
        """
        async with get_session(company_id=company_id) as session:
            # 1. Fetch and validate RFQ
            stmt_rfq = select(Rfq).where(Rfq.id == rfq_id)
            rfq = (await session.execute(stmt_rfq)).scalar_one_or_none()

            if not rfq:
                return {"success": False, "error": "طلب العروض غير موجود."}

            if rfq.status == RfqStatus.awarded:
                return {
                    "success": False,
                    "error": "تمت الترسية بالفعل على هذا الطلب.",
                }

            if rfq.status != RfqStatus.closed:
                current_status = (
                    rfq.status.value if hasattr(rfq.status, "value") else rfq.status
                )
                return {
                    "success": False,
                    "error": (
                        f"لا يمكن الترسية في الحالة الحالية ({current_status}). "
                        f"يجب إغلاق المهلة أولاً."
                    ),
                }

            # 2. Fetch and validate the selected quotation
            stmt_quotation = select(Quotation).where(
                Quotation.id == quotation_id,
                Quotation.rfq_id == rfq_id,
                Quotation.company_id == company_id,
            )
            selected_quotation = (
                await session.execute(stmt_quotation)
            ).scalar_one_or_none()

            if not selected_quotation:
                return {
                    "success": False,
                    "error": "العرض المختار غير موجود في هذا الطلب.",
                }

            if selected_quotation.status != QuotationStatus.submitted:
                current_status = (
                    selected_quotation.status.value
                    if hasattr(selected_quotation.status, "value")
                    else selected_quotation.status
                )
                return {
                    "success": False,
                    "error": (
                        f"العرض المختار في حالة ({current_status}) "
                        f"ولا يمكن اختياره."
                    ),
                }

            if selected_quotation.is_late:
                return {
                    "success": False,
                    "error": "لا يمكن اختيار عرض متأخر.",
                }

            # 3. Validate reason if required
            if reason_required and (not reason or not reason.strip()):
                return {
                    "success": False,
                    "error": "سبب الاختيار إلزامي وفق سياسة الشركة.",
                }

            # 4. Check if award already exists
            stmt_existing = select(AwardDecision).where(
                AwardDecision.rfq_id == rfq_id
            )
            existing_award = (
                await session.execute(stmt_existing)
            ).scalar_one_or_none()
            if existing_award:
                return {
                    "success": False,
                    "error": "يوجد قرار ترسية مسبق لهذا الطلب.",
                }

            # Save supplier_id before commit (avoid lazy-load issues)
            winner_supplier_id = selected_quotation.supplier_id

            # 5. Create the award decision
            award = AwardDecision(
                rfq_id=rfq_id,
                company_id=company_id,
                quotation_id=quotation_id,
                selected_by=selected_by,
                reason=reason,
                reason_required=reason_required,
            )
            session.add(award)

            # 6. Update winning quotation to 'accepted'
            selected_quotation.status = QuotationStatus.accepted

            # 7. Update all other submitted quotations to 'not_selected'
            stmt_other_quotations = select(Quotation).where(
                Quotation.rfq_id == rfq_id,
                Quotation.id != quotation_id,
                Quotation.status == QuotationStatus.submitted,
            )
            other_quotations = (
                await session.execute(stmt_other_quotations)
            ).scalars().all()

            for other in other_quotations:
                other.status = QuotationStatus.not_selected

                # AuditLog for each not_selected quotation
                audit_not_selected = AuditLog(
                    entity_table="quotations",
                    entity_id=other.id,
                    action=AuditAction.STATUS_CHANGE,
                    actor_user_id=selected_by,
                    company_id=company_id,
                    reason="Not selected in award decision",
                    old_data={"status": QuotationStatus.submitted.value},
                    new_data={"status": QuotationStatus.not_selected.value},
                )
                session.add(audit_not_selected)

            # 8. Update RFQ status to 'awarded'
            rfq.status = RfqStatus.awarded

            # 9. Update Purchase Request status to 'awarded'
            stmt_request = select(PurchaseRequest).where(
                PurchaseRequest.id == rfq.request_id
            )
            request = (await session.execute(stmt_request)).scalar_one_or_none()
            if request:
                request.status = RequestStatus.awarded

            # 10. AuditLog for the award itself
            audit_award = AuditLog(
                entity_table="award_decisions",
                entity_id=0,  # award.id not yet available; use 0 as placeholder
                action=AuditAction.AWARD,
                actor_user_id=selected_by,
                company_id=company_id,
                reason=reason,
                new_data={
                    "rfq_id": rfq_id,
                    "quotation_id": quotation_id,
                    "supplier_id": winner_supplier_id,
                    "reason": reason,
                },
            )
            session.add(audit_award)

            # Commit all changes in one transaction
            await session.commit()
            await session.refresh(award)

            logger.info(
                "AWARD | rfq_id=%s | quotation_id=%s | supplier_id=%s | selected_by=%s | not_selected_count=%s",
                rfq_id,
                quotation_id,
                winner_supplier_id,
                selected_by,
                len(other_quotations),
                extra={"security": True},
            )

            return {
                "success": True,
                "award_id": award.id,
                "rfq_id": rfq_id,
                "quotation_id": quotation_id,
                "supplier_id": winner_supplier_id,
                "not_selected_count": len(other_quotations),
            }

    # ========================================================================
    # 3. GET AWARD DECISION
    # ========================================================================

    @staticmethod
    async def get_award_decision(
        rfq_id: int,
        company_id: int,
    ) -> Optional[AwardDecision]:
        """Fetch the award decision for a specific RFQ."""
        async with get_session(company_id=company_id) as session:
            stmt = select(AwardDecision).where(AwardDecision.rfq_id == rfq_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    # ========================================================================
    # 4. GET AWARDED QUOTATION DETAILS
    # ========================================================================

    @staticmethod
    async def get_awarded_quotation_details(
        rfq_id: int,
        company_id: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Get full details of the awarded quotation.
        Used for notifying the winning supplier and for contract/execution
        reference.
        """
        async with get_session(company_id=company_id) as session:
            # Get award decision
            stmt_award = select(AwardDecision).where(
                AwardDecision.rfq_id == rfq_id
            )
            award = (await session.execute(stmt_award)).scalar_one_or_none()

            if not award:
                return None

            # Get quotation with lines
            stmt_quotation = select(Quotation).where(
                Quotation.id == award.quotation_id
            )
            quotation = (
                await session.execute(stmt_quotation)
            ).scalar_one_or_none()

            if not quotation:
                return None

            # Get quotation lines with snapshots
            stmt_lines = (
                select(QuotationLine, QuotationSnapshot)
                .outerjoin(
                    QuotationSnapshot,
                    QuotationSnapshot.quotation_line_id == QuotationLine.id,
                )
                .where(QuotationLine.quotation_id == quotation.id)
                .order_by(QuotationLine.line_no.asc())
            )
            lines_result = await session.execute(stmt_lines)
            lines = lines_result.all()

            return {
                "award": award,
                "quotation": quotation,
                "lines": [
                    {"line": line, "snapshot": snapshot}
                    for line, snapshot in lines
                ],
            }


# Singleton instance
award_service = AwardService()
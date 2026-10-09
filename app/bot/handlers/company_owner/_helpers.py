"""
Shared helpers for company_owner handlers.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.
"""
from typing import List, Optional
from datetime import datetime, timezone

from sqlalchemy import select

from aiogram.filters import BaseFilter

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session

logger = get_logger(__name__)


# ============================================================================
# Default reason when user skips
# ============================================================================
DEFAULT_SKIP_REASON = "لم يتم تحديد سبب"


# ============================================================================
# Available roles for employee invitations within a company
# ============================================================================
EMPLOYEE_ROLES_FOR_INVITE = [
    ("manager", "مدير مشتريات"),
    ("accountant", "محاسب"),
    ("storekeeper", "أمين مستودع"),
    ("requester", "طالب شراء"),
]


# ============================================================================
# Custom filter: buyer-side users only
# ============================================================================
class IsBuyerSide(BaseFilter):
    """
    Allow only users whose roles belong to the buyer (company) side.
    """
    async def __call__(self, event, roles: Optional[List[str]] = None, **kwargs) -> bool:
        return bool(roles) and permissions.is_buyer_side(roles)


# ============================================================================
# Helper: Normalize Phone Number
# ============================================================================
def normalize_phone_number(phone: str) -> str:
    """
    Normalize phone number to digits only.
    Removes +, -, spaces, parentheses.
    Example: "+7 (900) 123-45-67" → "79001234567"
    """
    if not phone:
        return ""
    return ''.join(c for c in phone if c.isdigit())


# ============================================================================
# Permission helpers
# ============================================================================
def require_company_owner_or_manager(roles: List[str]) -> bool:
    """Check if the user has company_owner or manager role."""
    return permissions.is_company_owner(roles) or permissions.is_manager(roles)


def require_buyer_admin_access(roles: List[str]) -> bool:
    """
    Allows Company Owner, Manager, AND Accountant to view lists and reports.
    """
    return (
        permissions.is_company_owner(roles) or
        permissions.is_manager(roles) or
        permissions.is_accountant(roles)
    )


# ============================================================================
# Helper: Execute PR Decision (shared logic for reject/revision)
# ============================================================================
async def _execute_pr_decision(
    message_or_callback,
    bot,
    user,
    request_id: int,
    company_id: int,
    new_status_str: str,
    reason: str,
    decision_label: str,
    decision_emoji: str,
    notification_template: str,
):
    """
    Shared logic to execute a purchase request decision.
    Handles DB update, requester notification, and confirmation message.
    """
    from app.database.models.purchase_request import PurchaseRequest
    from app.database.models.user import User
    from app.database.models.audit_log import AuditLog
    from app.database.models.enums import RequestStatus as ReqStatusEnum, AuditAction

    status_map = {
        "approved": ReqStatusEnum.approved,
        "rejected": ReqStatusEnum.rejected,
        "needs_revision": ReqStatusEnum.needs_revision,
    }
    new_status = status_map.get(new_status_str)
    if not new_status:
        return False, "حالة غير صالحة"

    async with get_session(company_id=company_id) as session:
        stmt = select(PurchaseRequest).where(
            PurchaseRequest.id == request_id,
            PurchaseRequest.company_id == company_id
        )
        result = await session.execute(stmt)
        request = result.scalar_one_or_none()

        if not request:
            return False, "لم يتم العثور على الطلب"

        current_status = request.status.value if hasattr(request.status, 'value') else str(request.status)
        if current_status not in ["submitted", "under_review"]:
            return False, "لا يمكن اتخاذ قرار على هذا الطلب في حالته الحالية"

        old_status_value = current_status

        request.status = new_status
        request.decided_by = user.id
        request.decided_at = datetime.now(timezone.utc)
        request.decision_note = reason

        # AuditLog for the decision
        audit = AuditLog(
            entity_table="purchase_requests",
            entity_id=request.id,
            action=AuditAction.STATUS_CHANGE,
            actor_user_id=user.id,
            company_id=company_id,
            reason=reason,
            old_data={"status": old_status_value},
            new_data={"status": new_status.value},
        )
        session.add(audit)

        await session.commit()

        requester_tg_id = None
        requester_name = "مقدم الطلب"
        async with get_session(company_id=company_id) as session2:
            stmt_user = select(User).where(User.id == request.requester_id)
            result_user = await session2.execute(stmt_user)
            requester = result_user.scalar_one_or_none()
            if requester:
                requester_tg_id = requester.telegram_user_id
                requester_name = requester.full_name

    if requester_tg_id:
        try:
            await bot.send_message(
                chat_id=requester_tg_id,
                text=notification_template.format(
                    title=request.title,
                    request_id=request.id,
                    decided_by=user.full_name,
                    reason=reason,
                ),
                parse_mode="HTML"
            )
        except Exception:
            logger.exception(
                "Could not notify requester | request_id=%s",
                request.id,
            )

    return True, request
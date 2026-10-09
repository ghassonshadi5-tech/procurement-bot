"""
Centralized permission checks for user roles (RBAC).
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

The UserMiddleware injects `data['roles']` as a List[str] of role codes.
These functions take that list and evaluate permissions based on the
Permissions Matrix defined in the project documentation.

9 System Roles:
- Platform: super_admin
- Buyer: company_owner, manager, accountant, storekeeper, requester
- Supplier: supplier_owner, supplier_manager, supplier_employee

NOTE:
- `is_manager_or_above` was removed (unused, duplicated
  `is_company_owner_or_manager`).
- `can_confirm_goods_receipt` intentionally keeps `manager` (decision
  made by project owner, diverges from the strict matrix).

CHANGE LOG:
- Fixed event.answer() on Message → now uses event.reply() for Message events.
- Added defensive isinstance check for roles in require_role decorator.
"""
from functools import wraps
from typing import List

from aiogram.types import CallbackQuery, Message

from app.core.logger import get_logger


logger = get_logger(__name__)


# ============================================================================
# 1. ROLE IDENTIFICATION (Individual Role Checks)
# ============================================================================

def is_super_admin(roles: List[str]) -> bool:
    """Check if user is the Platform Super Admin."""
    return "super_admin" in roles


def is_company_owner(roles: List[str]) -> bool:
    """Check if user is a Company Owner (Buyer side)."""
    return "company_owner" in roles


def is_manager(roles: List[str]) -> bool:
    """Check if user is a Procurement Manager (Buyer side)."""
    return "manager" in roles


def is_accountant(roles: List[str]) -> bool:
    """Check if user is an Accountant (Buyer side)."""
    return "accountant" in roles


def is_storekeeper(roles: List[str]) -> bool:
    """Check if user is a Storekeeper (Buyer side)."""
    return "storekeeper" in roles


def is_requester(roles: List[str]) -> bool:
    """Check if user is a Regular Employee / Requester (Buyer side)."""
    return "requester" in roles


def is_supplier_owner(roles: List[str]) -> bool:
    """Check if user is a Supplier Owner."""
    return "supplier_owner" in roles


def is_supplier_manager(roles: List[str]) -> bool:
    """Check if user is a Supplier Manager."""
    return "supplier_manager" in roles


def is_supplier_employee(roles: List[str]) -> bool:
    """Check if user is a Supplier Employee."""
    return "supplier_employee" in roles


# ============================================================================
# 2. SCOPE IDENTIFICATION (Which side does the user belong to?)
# ============================================================================

def is_buyer_side(roles: List[str]) -> bool:
    """Check if user has any Buyer (Company) role."""
    buyer_roles = {"company_owner", "manager", "accountant", "storekeeper", "requester"}
    return bool(buyer_roles.intersection(roles))


def is_supplier_side(roles: List[str]) -> bool:
    """Check if user has any Supplier role."""
    supplier_roles = {"supplier_owner", "supplier_manager", "supplier_employee"}
    return bool(supplier_roles.intersection(roles))


def is_registered(roles: List[str]) -> bool:
    """Check if the user has at least one valid role in the system."""
    all_valid_roles = {
        "super_admin",
        "company_owner", "manager", "accountant", "storekeeper", "requester",
        "supplier_owner", "supplier_manager", "supplier_employee",
    }
    return bool(all_valid_roles.intersection(roles))


# ============================================================================
# 3. BUSINESS LOGIC PERMISSIONS (Based on Permissions Matrix)
# ============================================================================

def is_company_owner_or_manager(roles: List[str]) -> bool:
    """
    Check if user is a Company Owner OR Manager.

    Used extensively in the Company Owner Panel for operations like:
    viewing RFQs, managing suppliers, viewing reports, managing employees
    and invitations.

    Matrix Reference:
    - Company Owner: Full access to all company operations
    - Manager: Full access to operational workflows (RFQs, requests, reports)
    """
    return is_company_owner(roles) or is_manager(roles)


# --- Purchase Requests ---

def can_create_purchase_request(roles: List[str]) -> bool:
    """
    Check if user can create a purchase request.
    Matrix: Requester (✅), Owner (◐), Manager (◐).
    """
    return is_requester(roles) or is_company_owner(roles) or is_manager(roles)


def can_edit_own_purchase_request(roles: List[str]) -> bool:
    """
    Check if user can edit their own purchase request (draft / needs_revision).
    Matrix: Requester (✅), Owner (◐), Manager (◐).
    """
    return is_requester(roles) or is_company_owner(roles) or is_manager(roles)


def can_approve_requests(roles: List[str]) -> bool:
    """
    Check if user can approve/reject purchase requests.
    Matrix: Owner (✅), Manager (✅ partial/approve), Requester (✖).
    """
    return is_company_owner(roles) or is_manager(roles)


# --- RFQ ---

def can_create_rfq(roles: List[str]) -> bool:
    """
    Check if user can create and publish RFQs.
    Matrix: Owner (✅), Manager (✅).
    """
    return is_company_owner(roles) or is_manager(roles)


def can_select_winner(roles: List[str]) -> bool:
    """
    Check if user can make the final award decision (select winning quotation).
    Matrix: Owner (✅), Manager (✅).
    """
    return is_company_owner(roles) or is_manager(roles)


def can_award_rfq(roles: List[str]) -> bool:
    """
    Check if user can award an RFQ (alias of can_select_winner, kept
    separate for future divergence between "select" and "award").
    Matrix: Owner (✅), Manager (✅).
    """
    return is_company_owner(roles) or is_manager(roles)


# --- Company Users ---

def can_manage_company_users(roles: List[str]) -> bool:
    """
    Check if user can invite/manage company employees.
    Matrix: Owner (✅ full), Manager (read only).
    """
    return is_company_owner(roles)


# --- Goods Receipt ---

def can_confirm_goods_receipt(roles: List[str]) -> bool:
    """
    Check if user can confirm goods receipt and record variances.

    Matrix reference: Storekeeper (✅ full), Owner (◐ approve).
    NOTE: Manager is intentionally kept here per project-owner decision,
    even though the strict matrix does not grant it. Do not remove
    without an explicit decision.
    """
    return is_storekeeper(roles) or is_company_owner(roles) or is_manager(roles)


# --- Reports ---

def can_access_financial_reports(roles: List[str]) -> bool:
    """
    Check if user can view financial reports.
    Matrix: Owner (✅), Accountant (✅), Manager (◐).
    """
    return is_company_owner(roles) or is_accountant(roles) or is_manager(roles)


def can_view_reports(roles: List[str]) -> bool:
    """
    Check if user can view general reports (read-only).
    Matrix: Owner (✅), Manager (◐), Accountant (◐).
    """
    return is_company_owner(roles) or is_manager(roles) or is_accountant(roles)


# --- Supplier: Catalog ---

def can_view_supplier_catalog(roles: List[str]) -> bool:
    """
    Check if user can view the supplier's own catalog.
    Matrix: Supplier Owner (✅), Supplier Manager (✅), Supplier Employee (✅ read).
    """
    return (
        is_supplier_owner(roles)
        or is_supplier_manager(roles)
        or is_supplier_employee(roles)
    )


def can_edit_supplier_catalog(roles: List[str]) -> bool:
    """
    Check if user can create/edit items in the supplier catalog.
    Matrix: Supplier Owner (✅ full), Supplier Manager (✅ partial).
    Supplier Employee only prepares (not implemented here).
    """
    return is_supplier_owner(roles) or is_supplier_manager(roles)


def can_manage_supplier_catalog(roles: List[str]) -> bool:
    """
    Backward-compatible alias of can_edit_supplier_catalog.
    Kept for existing callers.
    """
    return can_edit_supplier_catalog(roles)


# --- Supplier: Prices ---

def can_view_supplier_prices(roles: List[str]) -> bool:
    """
    Check if user can view the supplier's prices.
    Matrix: Supplier Owner (✅), Supplier Manager (✅), Supplier Employee (✅ read).
    """
    return (
        is_supplier_owner(roles)
        or is_supplier_manager(roles)
        or is_supplier_employee(roles)
    )


def can_edit_supplier_prices(roles: List[str]) -> bool:
    """
    Check if user can create/edit price rules and tiers.
    Matrix: Supplier Owner (✅ full), Supplier Manager (◐).
    """
    return is_supplier_owner(roles) or is_supplier_manager(roles)


def can_approve_supplier_price(roles: List[str]) -> bool:
    """
    Check if user can approve/activate a price rule.
    Matrix: Supplier Owner (✅).
    """
    return is_supplier_owner(roles)


# --- Supplier: Quotations ---

def can_submit_quotation(roles: List[str]) -> bool:
    """
    Check if user can submit a quotation in response to an RFQ.
    Matrix: Supplier Owner (✅), Supplier Manager (✅),
    Supplier Employee (prepare only — approval not granted here).
    """
    return is_supplier_owner(roles) or is_supplier_manager(roles)


def can_approve_quotation(roles: List[str]) -> bool:
    """
    Check if user can finalize/approve a quotation before submission.
    Matrix: Supplier Owner (✅).
    """
    return is_supplier_owner(roles)


# ============================================================================
# 4. HANDLER DECORATOR (Aiogram)
# ============================================================================

def require_role(*allowed_roles: str):
    """
    Decorator to restrict handler access to specific roles.

    On denial:
    - Logs a warning (with the allowed roles and the user's roles).
    - Replies to the user with a friendly message.
    - Returns without executing the handler.

    Usage:
        @require_role("company_owner", "manager")
        async def approve_handler(
            callback: CallbackQuery,
            roles: List[str],
            **kwargs
        ):
            ...
    """
    def decorator(handler):
        @wraps(handler)
        async def wrapper(
            event: Message | CallbackQuery,
            roles: List[str],
            **kwargs,
        ):
            # Defensive: ensure roles is iterable
            if not roles or not isinstance(roles, (list, tuple, set)):
                roles = []

            if not any(role in roles for role in allowed_roles):
                logger.warning(
                    "Permission denied | handler=%s | required=%s | user_roles=%s",
                    handler.__name__,
                    list(allowed_roles),
                    roles,
                )
                msg = "⛔ ليس لديك صلاحية للوصول إلى هذه الوظيفة."
                if isinstance(event, CallbackQuery):
                    await event.answer(msg, show_alert=True)
                elif isinstance(event, Message):
                    await event.reply(msg)
                return
            return await handler(event, roles=roles, **kwargs)
        return wrapper
    return decorator
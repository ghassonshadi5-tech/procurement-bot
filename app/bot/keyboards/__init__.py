"""
Bot Keyboards Package.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Centralizes imports for all reply keyboards used in the bot handlers.
Supports 9 roles: Super Admin, 5 Buyer roles, 3 Supplier roles.

Example usage in handlers:
    from app.bot.keyboards import get_super_admin_keyboard, get_keyboard_by_roles
"""

from app.bot.keyboards.main_menu import (
    # Super Admin
    get_super_admin_keyboard,
    
    # Buyer Side (Company)
    get_company_owner_keyboard,
    get_accountant_keyboard,
    get_storekeeper_keyboard,
    get_requester_keyboard,
    
    # Supplier Side
    get_supplier_owner_keyboard,
    get_supplier_manager_keyboard,
    get_supplier_employee_keyboard,
    
    # Central Dispatcher
    get_keyboard_by_roles,
)

__all__ = [
    # Super Admin
    "get_super_admin_keyboard",
    
    # Buyer Side (Company)
    "get_company_owner_keyboard",
    "get_accountant_keyboard",
    "get_storekeeper_keyboard",
    "get_requester_keyboard",
    
    # Supplier Side
    "get_supplier_owner_keyboard",
    "get_supplier_manager_keyboard",
    "get_supplier_employee_keyboard",
    
    # Central Dispatcher
    "get_keyboard_by_roles",
]
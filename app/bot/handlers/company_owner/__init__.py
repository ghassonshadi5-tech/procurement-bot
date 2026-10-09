"""
Company Owner handlers package.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

This package replaces the old monolithic `company_owner.py` file.
Each sub-module owns a single responsibility:

- requests.py   → Purchase Request list + approval/rejection workflow (FSM)
- rfq.py        → RFQ list + details
- suppliers.py  → Supplier management + invitation flows
- employees.py  → Employee management + invitation flows
- reports.py    → Company reports + Excel export

The exported `router` aggregates all sub-routers. It must be registered
in main.py in the SAME position where the old `company_owner` router was.
"""
from aiogram import Router

from app.bot.handlers.company_owner.requests import router as requests_router
from app.bot.handlers.company_owner.rfq import router as rfq_router
from app.bot.handlers.company_owner.suppliers import router as suppliers_router
from app.bot.handlers.company_owner.employees import router as employees_router
from app.bot.handlers.company_owner.reports import router as reports_router

# Aggregate router
router = Router(name="company_owner")

# Order matters: more specific handlers first.
# Requests (specific buttons) → RFQ → Suppliers → Employees → Reports
router.include_router(requests_router)
router.include_router(rfq_router)
router.include_router(suppliers_router)
router.include_router(employees_router)
router.include_router(reports_router)

__all__ = ["router"]
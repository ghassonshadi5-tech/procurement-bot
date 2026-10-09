"""
Services package - Business logic layer.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

All services are async and use the get_session() context manager
for RLS isolation and proper transaction management.
"""

# Identity & Authorization Services
from app.services.user_service import user_service
from app.services.company_service import company_service
from app.services.invite_service import invite_service
from app.services.supplier_service import supplier_service

# Procurement Cycle Services
from app.services.purchase_request_service import purchase_request_service
from app.services.rfq_service import rfq_service
from app.services.quotation_service import quotation_service
from app.services.award_service import award_service
from app.services.goods_receipt_service import goods_receipt_service

# Catalog & Pricing Services
from app.services.catalog_service import catalog_service
from app.services.pricing_service import pricing_service

# Support Services
from app.services.notification_service import notification_service
from app.services.audit_service import audit_service


__all__ = [
    # Identity & Authorization
    "user_service",
    "company_service",
    "invite_service",
    "supplier_service",
    
    # Procurement Cycle
    "purchase_request_service",
    "rfq_service",
    "quotation_service",
    "award_service",
    "goods_receipt_service",
    
    # Catalog & Pricing
    "catalog_service",
    "pricing_service",
    
    # Support
    "notification_service",
    "audit_service",
]
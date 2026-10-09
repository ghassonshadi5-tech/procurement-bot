"""
Models package - imports all database models.
Aligned with Procurement Bot Schema v2.0.
"""

# Base and Mixins
from app.database.models.base import Base, IdentityMixin, TimestampMixin, AuditMixin

# Identity & Authorization Models
from app.database.models.role import Role, RoleScope
from app.database.models.user import User, UserStatus
from app.database.models.user_role import UserRole
from app.database.models.company import Company, CompanyStatus
from app.database.models.company_invitation import CompanyInvitation
from app.database.models.supplier import Supplier
from app.database.models.supplier_relation import SupplierRelation, RelationStatus

# Catalog & Pricing Models
from app.database.models.unit_of_measure import UnitOfMeasure
from app.database.models.category import Category
from app.database.models.item import Item
from app.database.models.price_rule import PriceRule
from app.database.models.price_tier import PriceTier
from app.database.models.price_change_log import PriceChangeLog

# Procurement Cycle Models
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.purchase_request_line import PurchaseRequestLine
from app.database.models.rfq import Rfq
from app.database.models.rfq_line import RfqLine
from app.database.models.rfq_supplier import RfqSupplier
from app.database.models.quotation import Quotation
from app.database.models.quotation_line import QuotationLine
from app.database.models.quotation_snapshot import QuotationSnapshot
from app.database.models.award_decision import AwardDecision
from app.database.models.goods_receipt import GoodsReceipt
from app.database.models.goods_receipt_line import GoodsReceiptLine

# Support & Audit Models
from app.database.models.attachment import Attachment
from app.database.models.notification import Notification
from app.database.models.audit_log import AuditLog

__all__ = [
    # Base
    "Base",
    "IdentityMixin",
    "TimestampMixin",
    "AuditMixin",
    
    # Identity & Authorization
    "Role", "RoleScope",
    "User", "UserStatus",
    "UserRole",
    "Company", "CompanyStatus",
    "CompanyInvitation",
    "Supplier",
    "SupplierRelation", "RelationStatus",
    
    # Catalog & Pricing
    "UnitOfMeasure",
    "Category",
    "Item",
    "PriceRule",
    "PriceTier",
    "PriceChangeLog",
    
    # Procurement Cycle
    "PurchaseRequest", "PurchaseRequestLine",
    "Rfq", "RfqLine", "RfqSupplier",
    "Quotation", "QuotationLine", "QuotationSnapshot",
    "AwardDecision",
    "GoodsReceipt", "GoodsReceiptLine",
    
    # Support & Audit
    "Attachment",
    "Notification",
    "AuditLog",
]
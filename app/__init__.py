"""
Quick DB Initialization Script for Local Testing.
⚠️ FOR DEVELOPMENT ONLY - Use Alembic migrations in production.

Fixes:
1. Uses DO $$ blocks with EXCEPTION handling for ENUMs (no transaction abort).
2. Uses select().where() instead of session.get() for non-PK lookups.
3. Proper transaction management.
"""
import asyncio
from sqlalchemy import text, select
from app.database.database import engine, async_session_factory
from app.database.models.base import Base

# Import ALL models to register them with Base.metadata
from app.database.models import (
    user, company, supplier, supplier_relation,
    role, user_role, company_invitation,
    unit_of_measure, category, item,
    price_rule, price_tier, price_change_log,
    purchase_request, purchase_request_line,
    rfq, rfq_line, rfq_supplier,
    quotation, quotation_line, quotation_snapshot,
    award_decision,
    goods_receipt, goods_receipt_line,
    attachment, notification, audit_log
)
from app.database.models.role import Role, RoleScope
from app.database.models.unit_of_measure import UnitOfMeasure
from app.config.settings import settings


async def create_enums():
    """
    Create ENUM types safely using DO $$ blocks with EXCEPTION handling.
    This prevents transaction abort when ENUM already exists.
    """
    enums_to_create = [
        ("role_scope", "'platform', 'buyer', 'supplier'"),
        ("company_status", "'pending', 'active', 'suspended', 'rejected'"),
        ("user_status", "'invited', 'active', 'blocked'"),
        ("request_status", "'draft', 'submitted', 'under_review', 'needs_revision', 'approved', 'rejected', 'cancelled', 'rfq_created', 'awarded', 'received', 'closed', 'archived'"),
        ("rfq_method", "'targeted', 'open', 'mixed'"),
        ("rfq_status", "'draft', 'published', 'closed', 'cancelled', 'awarded'"),
        ("quotation_status", "'draft', 'submitted', 'late', 'withdrawn', 'accepted', 'rejected', 'not_selected'"),
        ("invite_status", "'invited', 'viewed', 'responded', 'declined', 'expired'"),
        ("relation_status", "'pending', 'active', 'suspended', 'rejected'"),
        ("price_type", "'public', 'custom'"),
        ("price_status", "'draft', 'active', 'inactive', 'expired'"),
        ("audit_action", "'INSERT', 'UPDATE', 'DELETE', 'APPROVE', 'REJECT', 'AWARD', 'STATUS_CHANGE'"),
    ]
    
    async with engine.begin() as conn:
        for enum_name, values in enums_to_create:
            # Use DO $$ block with EXCEPTION to safely handle "already exists"
            sql = f"""
                DO $$ BEGIN
                    CREATE TYPE {enum_name} AS ENUM ({values});
                EXCEPTION WHEN duplicate_object THEN null;
                END $$;
            """
            try:
                await conn.execute(text(sql))
                print(f"✅ Created/Verified ENUM: {enum_name}")
            except Exception as e:
                print(f"❌ Error creating ENUM {enum_name}: {e}")


async def create_tables():
    """Create all tables from models."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ All tables created successfully")


async def seed_initial_data():
    """Seed roles and units of measure."""
    async with async_session_factory() as session:
        # Seed 9 Roles
        roles_data = [
            ("super_admin", "مدير المنصة", RoleScope.platform),
            ("company_owner", "مالك الشركة", RoleScope.buyer),
            ("manager", "مدير المشتريات", RoleScope.buyer),
            ("accountant", "محاسب", RoleScope.buyer),
            ("storekeeper", "أمين مستودع", RoleScope.buyer),
            ("requester", "موظف", RoleScope.buyer),
            ("supplier_owner", "مالك المورد", RoleScope.supplier),
            ("supplier_manager", "مدير المورد", RoleScope.supplier),
            ("supplier_employee", "موظف المورد", RoleScope.supplier),
        ]
        
        for code, name_ar, scope in roles_data:
            # ✅ Correct way: use select().where() for non-PK lookups
            stmt = select(Role).where(Role.code == code)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            
            if not existing:
                session.add(Role(code=code, name_ar=name_ar, scope=scope))
                print(f"✅ Seeded role: {code}")
            else:
                print(f"⏭️  Role already exists: {code}")
        
        # Seed Units of Measure
        uoms_data = [
            ("pcs", "قطعة", "штука", "piece"),
            ("kg", "كيلوغرام", "килограмм", "kilogram"),
            ("m", "متر", "метр", "meter"),
            ("l", "لتر", "литр", "liter"),
            ("box", "صندوق", "коробка", "box"),
        ]
        
        for code, ar, ru, en in uoms_data:
            # ✅ Correct way: use select().where() for non-PK lookups
            stmt = select(UnitOfMeasure).where(UnitOfMeasure.code == code)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            
            if not existing:
                session.add(UnitOfMeasure(code=code, name_ar=ar, name_ru=ru, name_en=en))
                print(f"✅ Seeded UOM: {code}")
            else:
                print(f"⏭️  UOM already exists: {code}")
        
        await session.commit()
        print("✅ Initial data seeded successfully")


async def main():
    print("🚀 Initializing Procurement Bot Database...")
    print(f"📡 Connecting to: {settings.DATABASE_URL[:50]}...")
    
    await create_enums()
    await create_tables()
    await seed_initial_data()
    
    print("\n🎉 Database initialization complete!")
    print("✅ You can now run: python main.py")


if __name__ == "__main__":
    asyncio.run(main())
"""
Catalog Service - Manages supplier catalogs, items, and categories.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- CRUD operations for items in supplier catalogs.
- Category management (hierarchical tree).
- Search and filtering of items.
- Permission-based visibility (buyers see only linked suppliers' catalogs).

Key Rules from Permissions Matrix:
- Supplier Owner: Full access to their catalog (read, create, edit, approve).
- Supplier Manager/Employee: Read + Create + Edit (limited).
- Company Owner/Manager/Requester: Read-only access to linked suppliers' catalogs.
- Items are NEVER physically deleted (logical deactivation only).
- Dynamic attributes stored in JSONB with GIN index for fast search.

IMPROVEMENTS (this revision):
- get_item_details() now rejects calls without a viewer scope
  (viewer_company_id=None AND viewer_supplier_id=None) to prevent
  cross-tenant leaks.
- list_supplier_items() search filter simplified to name-only (the
  previous JSONB condition was dead code behind `if False`). A future
  revision can wire in proper JSONB search using the existing GIN index.
- deactivate_item() no longer builds an unused QuotationLine query.
- Category/UoM helpers use silent=True (global reference data).
- Uses get_logger(__name__) and %s formatting.
"""
from typing import Optional, List, Dict, Any
from decimal import Decimal

from sqlalchemy import select, and_, or_, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.item import Item
from app.database.models.category import Category
from app.database.models.unit_of_measure import UnitOfMeasure
from app.database.models.supplier import Supplier
from app.database.models.supplier_relation import SupplierRelation, RelationStatus
from app.database.models.purchase_request_line import PurchaseRequestLine
from app.database.models.quotation_line import QuotationLine
from app.core.logger import get_logger


logger = get_logger(__name__)


class CatalogService:
    """
    Service class for Catalog operations.
    """

    # ========================================================================
    # 1. ITEM CRUD (Supplier-side operations)
    # ========================================================================

    @staticmethod
    async def create_item(
        supplier_id: int,
        name: str,
        uom_id: int,
        category_id: Optional[int] = None,
        sku: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a new item in the supplier's catalog.

        Args:
            supplier_id: The supplier who owns this item.
            name: Item name (mandatory).
            uom_id: Unit of measure ID.
            category_id: Optional category classification.
            sku: Optional Stock Keeping Unit (unique per supplier).
            attributes: Optional dynamic attributes (JSONB).

        Returns:
            {"success": True, "item": Item} or {"success": False, "error": str}
        """
        async with get_session(is_supplier_owner=True) as session:
            # 1. Validate name
            if not name or not name.strip():
                return {"success": False, "error": "اسم الصنف إلزامي."}

            name = name.strip()
            if len(name) < 2:
                return {
                    "success": False,
                    "error": "اسم الصنف قصير جداً (حرفان على الأقل).",
                }

            # 2. Validate UOM exists
            stmt_uom = select(UnitOfMeasure).where(UnitOfMeasure.id == uom_id)
            uom = (await session.execute(stmt_uom)).scalar_one_or_none()
            if not uom:
                return {"success": False, "error": "وحدة القياس غير موجودة."}

            # 3. Validate category exists (if provided)
            if category_id:
                stmt_cat = select(Category).where(Category.id == category_id)
                category = (await session.execute(stmt_cat)).scalar_one_or_none()
                if not category:
                    return {"success": False, "error": "التصنيف غير موجود."}

            # 4. Check SKU uniqueness per supplier (if provided)
            if sku:
                stmt_sku = select(Item).where(
                    Item.supplier_id == supplier_id,
                    Item.sku == sku,
                )
                existing_sku = (
                    await session.execute(stmt_sku)
                ).scalar_one_or_none()
                if existing_sku:
                    return {
                        "success": False,
                        "error": f"رمز SKU '{sku}' مستخدم بالفعل في كتالوجك.",
                    }

            # 5. Create the item
            new_item = Item(
                supplier_id=supplier_id,
                name=name,
                uom_id=uom_id,
                category_id=category_id,
                sku=sku,
                attributes=attributes or {},
                is_active=True,
            )
            session.add(new_item)

            try:
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to create item | supplier_id=%s | name=%s",
                    supplier_id,
                    name,
                )
                return {
                    "success": False,
                    "error": "تعذر إنشاء الصنف. قد يكون هناك تكرار في البيانات.",
                }

            await session.refresh(new_item)
            logger.info(
                "Created item | item_id=%s | name=%s | supplier_id=%s",
                new_item.id,
                name,
                supplier_id,
            )
            return {"success": True, "item": new_item}

    @staticmethod
    async def update_item(
        supplier_id: int,
        item_id: int,
        name: Optional[str] = None,
        uom_id: Optional[int] = None,
        category_id: Optional[int] = None,
        sku: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Update an existing item in the supplier's catalog.
        Only provided fields are updated (partial update).
        """
        async with get_session(is_supplier_owner=True) as session:
            # 1. Fetch item and validate ownership
            stmt = select(Item).where(
                Item.id == item_id,
                Item.supplier_id == supplier_id,
            )
            item = (await session.execute(stmt)).scalar_one_or_none()

            if not item:
                return {
                    "success": False,
                    "error": "الصنف غير موجود في كتالوجك.",
                }

            # 2. Validate and apply updates
            if name is not None:
                name = name.strip()
                if len(name) < 2:
                    return {"success": False, "error": "اسم الصنف قصير جداً."}
                item.name = name

            if uom_id is not None:
                stmt_uom = select(UnitOfMeasure).where(
                    UnitOfMeasure.id == uom_id
                )
                uom = (await session.execute(stmt_uom)).scalar_one_or_none()
                if not uom:
                    return {
                        "success": False,
                        "error": "وحدة القياس غير موجودة.",
                    }
                item.uom_id = uom_id

            if category_id is not None:
                if category_id == 0:  # 0 means "remove category"
                    item.category_id = None
                else:
                    stmt_cat = select(Category).where(
                        Category.id == category_id
                    )
                    category = (
                        await session.execute(stmt_cat)
                    ).scalar_one_or_none()
                    if not category:
                        return {
                            "success": False,
                            "error": "التصنيف غير موجود.",
                        }
                    item.category_id = category_id

            if sku is not None:
                # Check SKU uniqueness per supplier
                stmt_sku = select(Item).where(
                    Item.supplier_id == supplier_id,
                    Item.sku == sku,
                    Item.id != item_id,
                )
                existing_sku = (
                    await session.execute(stmt_sku)
                ).scalar_one_or_none()
                if existing_sku:
                    return {
                        "success": False,
                        "error": f"رمز SKU '{sku}' مستخدم بالفعل في كتالوجك.",
                    }
                item.sku = sku

            if attributes is not None:
                item.attributes = attributes

            await session.commit()
            await session.refresh(item)

            logger.info(
                "Updated item | item_id=%s | supplier_id=%s",
                item_id,
                supplier_id,
            )
            return {"success": True, "item": item}

    @staticmethod
    async def deactivate_item(
        supplier_id: int,
        item_id: int,
    ) -> Dict[str, Any]:
        """
        Deactivate an item (logical deletion).
        The item remains in the database but is hidden from catalogs.

        Historical references in purchase requests and quotations are
        preserved (they use snapshots). Deactivation is not blocked.
        """
        async with get_session(is_supplier_owner=True) as session:
            stmt = select(Item).where(
                Item.id == item_id,
                Item.supplier_id == supplier_id,
            )
            item = (await session.execute(stmt)).scalar_one_or_none()

            if not item:
                return {
                    "success": False,
                    "error": "الصنف غير موجود في كتالوجك.",
                }

            # Detect historical references (informational only)
            stmt_req_ref = (
                select(PurchaseRequestLine.id)
                .where(PurchaseRequestLine.item_id == item_id)
                .limit(1)
            )
            has_req_ref = (
                await session.execute(stmt_req_ref)
            ).scalar_one_or_none() is not None

            item.is_active = False
            await session.commit()

            warning = ""
            if has_req_ref:
                warning = (
                    " (تنبيه: هذا الصنف مرتبط بطلبات شراء سابقة، "
                    "وسيبقى في السجلات التاريخية)"
                )

            logger.info(
                "Deactivated item | item_id=%s | supplier_id=%s",
                item_id,
                supplier_id,
            )
            return {"success": True, "warning": warning}

    @staticmethod
    async def activate_item(
        supplier_id: int,
        item_id: int,
    ) -> Dict[str, Any]:
        """Reactivate a previously deactivated item."""
        async with get_session(is_supplier_owner=True) as session:
            stmt = select(Item).where(
                Item.id == item_id,
                Item.supplier_id == supplier_id,
            )
            item = (await session.execute(stmt)).scalar_one_or_none()

            if not item:
                return {
                    "success": False,
                    "error": "الصنف غير موجود في كتالوجك.",
                }

            item.is_active = True
            await session.commit()

            logger.info(
                "Activated item | item_id=%s | supplier_id=%s",
                item_id,
                supplier_id,
            )
            return {"success": True}

    # ========================================================================
    # 2. ITEM RETRIEVAL (Permission-based visibility)
    # ========================================================================

    @staticmethod
    async def get_item_details(
        item_id: int,
        viewer_company_id: Optional[int] = None,
        viewer_supplier_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get full details of an item with permission checks.

        SECURITY: The caller MUST declare a viewer scope. Calls with
        both viewer_company_id=None and viewer_supplier_id=None are
        rejected to prevent cross-tenant leaks.

        Visibility rules:
        - Supplier can see their own items.
        - Company can see items of suppliers they have an ACTIVE relation with.
        - No cross-company catalog access.
        """
        # --- Enforce viewer scope ---
        if viewer_company_id is None and viewer_supplier_id is None:
            logger.warning(
                "get_item_details called without a viewer scope "
                "| item_id=%s",
                item_id,
                extra={"security": True},
            )
            return None

        async with get_session(company_id=viewer_company_id, silent=viewer_company_id is None) as session:
            # Fetch item
            stmt = select(Item).where(Item.id == item_id)
            item = (await session.execute(stmt)).scalar_one_or_none()

            if not item:
                return None

            # Permission check
            if viewer_supplier_id:
                # Supplier viewing: must be their own item
                if item.supplier_id != viewer_supplier_id:
                    logger.warning(
                        "SECURITY: supplier tried to access another supplier's item"
                        " | viewer_supplier_id=%s | item_id=%s | owner_supplier_id=%s",
                        viewer_supplier_id,
                        item_id,
                        item.supplier_id,
                        extra={"security": True},
                    )
                    return None
            elif viewer_company_id:
                # Company viewing: must have active relation with the supplier
                stmt_relation = select(SupplierRelation).where(
                    SupplierRelation.company_id == viewer_company_id,
                    SupplierRelation.supplier_id == item.supplier_id,
                    SupplierRelation.status == RelationStatus.active,
                )
                relation = (
                    await session.execute(stmt_relation)
                ).scalar_one_or_none()

                if not relation:
                    logger.warning(
                        "SECURITY: company tried to access item without active relation"
                        " | viewer_company_id=%s | item_id=%s | supplier_id=%s",
                        viewer_company_id,
                        item_id,
                        item.supplier_id,
                        extra={"security": True},
                    )
                    return None

            # Fetch related data
            stmt_uom = select(UnitOfMeasure).where(
                UnitOfMeasure.id == item.uom_id
            )
            uom = (await session.execute(stmt_uom)).scalar_one_or_none()

            category = None
            if item.category_id:
                stmt_category = select(Category).where(
                    Category.id == item.category_id
                )
                category = (
                    await session.execute(stmt_category)
                ).scalar_one_or_none()

            stmt_supplier = select(Supplier).where(
                Supplier.id == item.supplier_id
            )
            supplier = (
                await session.execute(stmt_supplier)
            ).scalar_one_or_none()

            return {
                "item": item,
                "uom": uom,
                "category": category,
                "supplier": supplier,
                "attributes": item.attributes or {},
            }

    @staticmethod
    async def list_supplier_items(
        supplier_id: int,
        viewer_company_id: Optional[int] = None,
        viewer_supplier_id: Optional[int] = None,
        category_id: Optional[int] = None,
        search_query: Optional[str] = None,
        include_inactive: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        List items in a supplier's catalog with permission checks.

        Args:
            supplier_id: The supplier whose catalog to list.
            viewer_company_id: The company viewing (must have active relation).
            viewer_supplier_id: The supplier viewing (must be themselves).
            category_id: Filter by category.
            search_query: Search in item name.
            include_inactive: Include deactivated items (supplier view only).
            limit/offset: Pagination.

        Returns:
            {"items": [...], "total": int, "has_more": bool}
        """
        # --- Enforce viewer scope ---
        if viewer_company_id is None and viewer_supplier_id is None:
            logger.warning(
                "list_supplier_items called without a viewer scope "
                "| supplier_id=%s",
                supplier_id,
                extra={"security": True},
            )
            return {"items": [], "total": 0, "has_more": False}

        async with get_session(
            company_id=viewer_company_id,
            silent=viewer_company_id is None,
        ) as session:
            # Permission check
            if viewer_supplier_id:
                if supplier_id != viewer_supplier_id:
                    logger.warning(
                        "SECURITY: supplier tried to list another supplier's catalog"
                        " | viewer_supplier_id=%s | target_supplier_id=%s",
                        viewer_supplier_id,
                        supplier_id,
                        extra={"security": True},
                    )
                    return {"items": [], "total": 0, "has_more": False}
            elif viewer_company_id:
                stmt_relation = select(SupplierRelation).where(
                    SupplierRelation.company_id == viewer_company_id,
                    SupplierRelation.supplier_id == supplier_id,
                    SupplierRelation.status == RelationStatus.active,
                )
                relation = (
                    await session.execute(stmt_relation)
                ).scalar_one_or_none()
                if not relation:
                    logger.warning(
                        "SECURITY: company tried to list catalog without active relation"
                        " | viewer_company_id=%s | target_supplier_id=%s",
                        viewer_company_id,
                        supplier_id,
                        extra={"security": True},
                    )
                    return {"items": [], "total": 0, "has_more": False}

            # Build query
            stmt = select(Item).where(Item.supplier_id == supplier_id)

            # Active filter: suppliers may include inactive items;
            # companies always see only active items.
            if not include_inactive or viewer_company_id is not None:
                stmt = stmt.where(Item.is_active.is_(True))

            # Category filter
            if category_id:
                stmt = stmt.where(Item.category_id == category_id)

            # Search filter (name only for now; JSONB search via GIN
            # index can be added later if required).
            if search_query:
                search_term = f"%{search_query.strip()}%"
                stmt = stmt.where(Item.name.ilike(search_term))

            # Count total
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = (await session.execute(count_stmt)).scalar() or 0

            # Apply pagination and ordering (newest first)
            stmt = (
                stmt.order_by(Item.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            items = list(result.scalars().all())

            return {
                "items": items,
                "total": total,
                "has_more": (offset + limit) < total,
            }

    @staticmethod
    async def list_company_available_suppliers_items(
        company_id: int,
        search_query: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        List all items available to a company from all linked suppliers.
        This is used when creating purchase requests (item selection).
        """
        async with get_session(company_id=company_id) as session:
            # Get all active supplier relations
            stmt_relations = select(SupplierRelation).where(
                SupplierRelation.company_id == company_id,
                SupplierRelation.status == RelationStatus.active,
            )
            relations = (
                await session.execute(stmt_relations)
            ).scalars().all()

            if not relations:
                return []

            supplier_ids = [r.supplier_id for r in relations]

            # Fetch items from all linked suppliers
            stmt = (
                select(Item, Supplier.name)
                .join(Supplier, Item.supplier_id == Supplier.id)
                .where(
                    Item.supplier_id.in_(supplier_ids),
                    Item.is_active.is_(True),
                )
            )

            if search_query:
                stmt = stmt.where(Item.name.ilike(f"%{search_query}%"))

            stmt = stmt.order_by(Item.name.asc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.all()

            return [
                {"item": item, "supplier_name": supplier_name}
                for item, supplier_name in rows
            ]

    # ========================================================================
    # 3. CATEGORY MANAGEMENT
    # ========================================================================

    @staticmethod
    async def get_categories_tree() -> List[Dict[str, Any]]:
        """
        Get the full category tree (hierarchical).
        Categories are global (shared across all suppliers).
        """
        async with get_session(silent=True) as session:
            stmt = select(Category).order_by(Category.name_ar.asc())
            result = await session.execute(stmt)
            categories = list(result.scalars().all())

            # Build tree structure
            categories_by_id = {c.id: c for c in categories}
            tree = []

            for category in categories:
                if category.parent_id is None:
                    # Root category
                    tree.append(
                        {
                            "category": category,
                            "children": CatalogService._get_children(
                                category.id, categories_by_id
                            ),
                        }
                    )

            return tree

    @staticmethod
    def _get_children(
        parent_id: int,
        categories_by_id: Dict[int, Category],
    ) -> List[Dict[str, Any]]:
        """Recursively get child categories."""
        children = []
        for cat in categories_by_id.values():
            if cat.parent_id == parent_id:
                children.append(
                    {
                        "category": cat,
                        "children": CatalogService._get_children(
                            cat.id, categories_by_id
                        ),
                    }
                )
        return children

    @staticmethod
    async def create_category(
        name_ar: str,
        name_ru: Optional[str] = None,
        name_en: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create a new category (Super Admin operation).
        Categories are global reference data.
        """
        async with get_session(silent=True) as session:
            # Validate name
            if not name_ar or not name_ar.strip():
                return {"success": False, "error": "الاسم العربي إلزامي."}

            # Validate parent exists (if provided)
            if parent_id:
                stmt_parent = select(Category).where(
                    Category.id == parent_id
                )
                parent = (
                    await session.execute(stmt_parent)
                ).scalar_one_or_none()
                if not parent:
                    return {
                        "success": False,
                        "error": "التصنيف الأب غير موجود.",
                    }

            # Create category
            new_category = Category(
                name_ar=name_ar.strip(),
                name_ru=name_ru,
                name_en=name_en,
                parent_id=parent_id,
            )
            session.add(new_category)

            try:
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to create category | name_ar=%s", name_ar
                )
                return {
                    "success": False,
                    "error": "تعذر إنشاء التصنيف.",
                }

            await session.refresh(new_category)
            logger.info(
                "Created category | category_id=%s | name_ar=%s",
                new_category.id,
                name_ar,
            )
            return {"success": True, "category": new_category}

    # ========================================================================
    # 4. UNIT OF MEASURE (Reference data)
    # ========================================================================

    @staticmethod
    async def get_all_units_of_measure() -> List[UnitOfMeasure]:
        """
        Get all available units of measure.
        UoM are global reference data.
        """
        async with get_session(silent=True) as session:
            stmt = select(UnitOfMeasure).order_by(UnitOfMeasure.code.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ========================================================================
    # 5. SEARCH (Advanced)
    # ========================================================================

    @staticmethod
    async def search_items_for_company(
        company_id: int,
        query: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search for items across all linked suppliers' catalogs.
        Used in purchase request creation for quick item lookup.
        """
        if not query or len(query.strip()) < 2:
            return []

        async with get_session(company_id=company_id) as session:
            # Get active supplier relations
            stmt_relations = select(SupplierRelation.supplier_id).where(
                SupplierRelation.company_id == company_id,
                SupplierRelation.status == RelationStatus.active,
            )
            result_relations = await session.execute(stmt_relations)
            supplier_ids = [row[0] for row in result_relations.all()]

            if not supplier_ids:
                return []

            # Search items
            search_term = f"%{query.strip()}%"
            stmt = (
                select(Item, Supplier.name.label("supplier_name"))
                .join(Supplier, Item.supplier_id == Supplier.id)
                .where(
                    Item.supplier_id.in_(supplier_ids),
                    Item.is_active.is_(True),
                    or_(
                        Item.name.ilike(search_term),
                        Item.sku.ilike(search_term),
                    ),
                )
                .order_by(Item.name.asc())
                .limit(limit)
            )

            result = await session.execute(stmt)
            rows = result.all()

            return [
                {
                    "item_id": item.id,
                    "name": item.name,
                    "sku": item.sku,
                    "supplier_id": item.supplier_id,
                    "supplier_name": supplier_name,
                }
                for item, supplier_name in rows
            ]


# Singleton instance
catalog_service = CatalogService()
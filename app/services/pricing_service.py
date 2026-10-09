"""
Pricing Service - Central pricing engine for the Procurement Bot.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

This is the SINGLE SOURCE OF TRUTH for all pricing logic.
No pricing logic should exist in Handlers, Routers, or any other service.

Resolution Priority (fn_resolve_price):
1. Custom price (valid, active, for this specific company)
2. Public price (valid, active)
3. Quantity tier (applied to the resolved base price)

Key Rules:
- NEVER guess a price when data is missing or ambiguous.
- Overlapping tiers or rules → return "price unavailable".
- Custom prices are isolated per company.
- Supplier owner can see all their prices (public + all custom).
- All price changes are logged via DB trigger (log_price_change).

IMPROVEMENTS (this revision):
- Removed get_price_change_history() — it duplicated
  audit_service.get_price_change_history() with fewer safeguards
  (no scope, no silent). Use audit_service for history reads.
- All unscoped sessions use silent=True (price rules live in a
  cross-tenant table; scope is enforced by supplier_id in code).
- resolve_price() uses silent=True when company_id is None.
- Uses get_logger(__name__) and %s formatting.
- Uses .is_(None) instead of == None.
- NOTE: RLS policies on price_rules are not yet implemented in the
  database. Until they are, isolation relies on the supplier_id
  parameters passed by callers. A future migration will add RLS
  and this service may be upgraded to is_supplier_owner=True.
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.price_rule import PriceRule
from app.database.models.price_tier import PriceTier
from app.database.models.item import Item
from app.database.models.supplier import Supplier
from app.database.models.supplier_relation import SupplierRelation, RelationStatus
from app.database.models.enums import PriceType, PriceStatus
from app.core.logger import get_logger


logger = get_logger(__name__)


class PriceResolutionResult:
    """
    Represents the result of a price resolution attempt.
    """

    def __init__(
        self,
        available: bool,
        unit_price: Optional[Decimal] = None,
        currency: Optional[str] = None,
        price_type: Optional[str] = None,
        price_basis: Optional[str] = None,
        price_rule_id: Optional[int] = None,
        price_tier_id: Optional[int] = None,
        effective_from: Optional[datetime] = None,
        effective_to: Optional[datetime] = None,
        reason: Optional[str] = None,
    ):
        self.available = available
        self.unit_price = unit_price
        self.currency = currency
        self.price_type = price_type  # 'public' or 'custom'
        self.price_basis = price_basis  # 'public', 'custom', 'tier', 'manual'
        self.price_rule_id = price_rule_id
        self.price_tier_id = price_tier_id
        self.effective_from = effective_from
        self.effective_to = effective_to
        self.reason = reason  # Reason if not available

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "unit_price": str(self.unit_price) if self.unit_price else None,
            "currency": self.currency,
            "price_type": self.price_type,
            "price_basis": self.price_basis,
            "price_rule_id": self.price_rule_id,
            "price_tier_id": self.price_tier_id,
            "effective_from": self.effective_from.isoformat()
            if self.effective_from
            else None,
            "effective_to": self.effective_to.isoformat()
            if self.effective_to
            else None,
            "reason": self.reason,
        }


class PricingService:
    """
    Central Pricing Service.
    All pricing logic in the system MUST go through this service.
    """

    # ========================================================================
    # 1. PRICE RESOLUTION (Core Engine)
    # ========================================================================

    @staticmethod
    async def resolve_price(
        supplier_id: int,
        item_id: int,
        quantity: Decimal,
        company_id: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> PriceResolutionResult:
        """
        Resolve the applicable price for a given item, supplier, quantity,
        and company.

        Resolution Priority:
        1. Custom price (if company_id provided and a valid custom rule exists)
        2. Public price (fallback)
        3. Quantity tier (applied to the resolved base price)

        Args:
            supplier_id: The supplier who owns the item.
            item_id: The item to price.
            quantity: The requested quantity (for tier resolution).
            company_id: The buyer company (None = public price only).
            as_of: The reference time for validity check (default: now).

        Returns:
            PriceResolutionResult with the resolved price or unavailability
            reason.
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)

        # Use silent=True when there's no company scope (bootstrap/platform
        # use). When company_id is present, RLS context is applied.
        if company_id is not None:
            ctx = get_session(company_id=company_id)
        else:
            ctx = get_session(silent=True)

        async with ctx as session:
            # Step 1: Try to find a valid CUSTOM price for this company
            custom_rule = None
            if company_id is not None:
                custom_rule = await PricingService._find_valid_price_rule(
                    session=session,
                    supplier_id=supplier_id,
                    item_id=item_id,
                    price_type=PriceType.custom,
                    company_id=company_id,
                    as_of=as_of,
                )

            # Step 2: If no custom price, try PUBLIC price
            public_rule = None
            if custom_rule is None:
                public_rule = await PricingService._find_valid_price_rule(
                    session=session,
                    supplier_id=supplier_id,
                    item_id=item_id,
                    price_type=PriceType.public,
                    company_id=None,
                    as_of=as_of,
                )

            # Step 3: Determine which rule to use
            selected_rule = custom_rule if custom_rule else public_rule

            if selected_rule is None:
                return PriceResolutionResult(
                    available=False,
                    reason=(
                        "السعر غير متاح: لا توجد قاعدة سعر صالحة لهذا البند."
                    ),
                )

            # Step 4: Apply quantity tier if applicable
            tier_result = await PricingService._resolve_tier(
                session=session,
                price_rule_id=selected_rule.id,
                quantity=quantity,
            )

            if tier_result["error"]:
                # Overlapping or ambiguous tiers → do NOT guess
                return PriceResolutionResult(
                    available=False,
                    reason=f"السعر غير متاح: {tier_result['error']}",
                )

            # Step 5: Determine final price
            if tier_result["tier"]:
                tier = tier_result["tier"]
                final_price = tier.unit_price
                price_basis = "tier"
                price_tier_id = tier.id
            else:
                final_price = selected_rule.base_price
                price_basis = selected_rule.price_type.value
                price_tier_id = None

            return PriceResolutionResult(
                available=True,
                unit_price=final_price,
                currency=selected_rule.currency,
                price_type=selected_rule.price_type.value,
                price_basis=price_basis,
                price_rule_id=selected_rule.id,
                price_tier_id=price_tier_id,
                effective_from=selected_rule.effective_from,
                effective_to=selected_rule.effective_to,
            )

    # ========================================================================
    # 2. INTERNAL: Find Valid Price Rule
    # ========================================================================

    @staticmethod
    async def _find_valid_price_rule(
        session: AsyncSession,
        supplier_id: int,
        item_id: int,
        price_type: PriceType,
        company_id: Optional[int],
        as_of: datetime,
    ) -> Optional[PriceRule]:
        """
        Find a valid, active price rule matching the criteria.
        Returns None if no valid rule exists or if multiple conflicting
        rules exist.
        """
        stmt = select(PriceRule).where(
            PriceRule.supplier_id == supplier_id,
            PriceRule.item_id == item_id,
            PriceRule.price_type == price_type,
            PriceRule.status == PriceStatus.active,
            PriceRule.effective_from <= as_of,
            or_(
                PriceRule.effective_to.is_(None),
                PriceRule.effective_to > as_of,
            ),
        )

        if price_type == PriceType.custom:
            stmt = stmt.where(PriceRule.company_id == company_id)
        else:
            stmt = stmt.where(PriceRule.company_id.is_(None))

        result = await session.execute(stmt)
        rules = result.scalars().all()

        if len(rules) == 0:
            return None

        if len(rules) > 1:
            # Multiple active rules for the same criteria = ambiguity
            # Do NOT guess. Log and return None.
            logger.error(
                "PRICING AMBIGUITY: multiple active rules match "
                "| price_type=%s | supplier_id=%s | item_id=%s | company_id=%s | count=%s",
                price_type.value,
                supplier_id,
                item_id,
                company_id,
                len(rules),
            )
            return None

        return rules[0]

    # ========================================================================
    # 3. INTERNAL: Resolve Quantity Tier
    # ========================================================================

    @staticmethod
    async def _resolve_tier(
        session: AsyncSession,
        price_rule_id: int,
        quantity: Decimal,
    ) -> Dict[str, Any]:
        """
        Find the applicable quantity tier for a given quantity.

        Returns:
            {"tier": PriceTier or None, "error": str or None}

        If multiple tiers match (overlap), returns an error (do NOT guess).
        If no tier matches, returns None (use base price).
        """
        stmt = select(PriceTier).where(
            PriceTier.price_rule_id == price_rule_id,
            PriceTier.min_qty <= quantity,
        )
        result = await session.execute(stmt)
        candidates = result.scalars().all()

        if not candidates:
            return {"tier": None, "error": None}

        # Filter candidates: quantity must be within [min_qty, max_qty]
        matching_tiers = []
        for tier in candidates:
            if tier.max_qty is None or quantity <= tier.max_qty:
                matching_tiers.append(tier)

        if len(matching_tiers) == 0:
            return {"tier": None, "error": None}

        if len(matching_tiers) > 1:
            # Overlapping tiers! This should be prevented by EXCLUDE
            # constraint, but if it happens, do NOT guess.
            logger.error(
                "TIER OVERLAP: multiple tiers match a single quantity "
                "| price_rule_id=%s | quantity=%s | count=%s",
                price_rule_id,
                quantity,
                len(matching_tiers),
            )
            return {
                "tier": None,
                "error": "تداخل في شرائح الكميات. يرجى مراجعة المورد.",
            }

        return {"tier": matching_tiers[0], "error": None}

    # ========================================================================
    # 4. PRICE RULE MANAGEMENT (CRUD for Supplier)
    # ========================================================================

    @staticmethod
    async def create_price_rule(
        supplier_id: int,
        item_id: int,
        price_type: PriceType,
        base_price: Decimal,
        currency: str,
        effective_from: datetime,
        effective_to: Optional[datetime] = None,
        company_id: Optional[int] = None,
        created_by: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create a new price rule (draft status by default).
        Requires approval before activation.
        """
        async with get_session(silent=True) as session:
            # Validate price_type and company_id consistency
            if price_type == PriceType.custom and company_id is None:
                return {
                    "success": False,
                    "error": "السعر المخصص يتطلب تحديد الشركة.",
                }
            if price_type == PriceType.public and company_id is not None:
                return {
                    "success": False,
                    "error": "السعر العام لا يرتبط بشركة محددة.",
                }

            # Validate base_price
            if base_price <= 0:
                return {
                    "success": False,
                    "error": "السعر الأساسي يجب أن يكون أكبر من صفر.",
                }

            # Validate currency format
            if (
                len(currency) != 3
                or not currency.isupper()
                or not currency.isalpha()
            ):
                return {
                    "success": False,
                    "error": (
                        "رمز العملة يجب أن يكون 3 أحرف كبيرة "
                        "(مثل USD, EUR, SAR)."
                    ),
                }

            # Validate effective dates
            if effective_to and effective_to <= effective_from:
                return {
                    "success": False,
                    "error": (
                        "تاريخ انتهاء الصلاحية يجب أن يكون بعد تاريخ البدء."
                    ),
                }

            # Create the rule
            new_rule = PriceRule(
                supplier_id=supplier_id,
                item_id=item_id,
                price_type=price_type,
                company_id=company_id,
                currency=currency,
                base_price=base_price,
                effective_from=effective_from,
                effective_to=effective_to,
                status=PriceStatus.draft,
                version=1,
            )
            session.add(new_rule)
            await session.commit()
            await session.refresh(new_rule)

            logger.info(
                "Created price rule | price_rule_id=%s | price_type=%s | item_id=%s",
                new_rule.id,
                price_type.value,
                item_id,
            )
            return {"success": True, "price_rule": new_rule}

    @staticmethod
    async def activate_price_rule(
        price_rule_id: int,
        approved_by: int,
        supplier_id: int,
    ) -> Dict[str, Any]:
        """
        Activate a price rule (requires approval).
        The DB EXCLUDE constraint will prevent overlapping active rules.
        """
        async with get_session(silent=True) as session:
            stmt = select(PriceRule).where(
                PriceRule.id == price_rule_id,
                PriceRule.supplier_id == supplier_id,
            )
            rule = (await session.execute(stmt)).scalar_one_or_none()

            if not rule:
                return {"success": False, "error": "قاعدة السعر غير موجودة."}

            if rule.status == PriceStatus.active:
                return {
                    "success": False,
                    "error": "قاعدة السعر نشطة بالفعل.",
                }

            if rule.status == PriceStatus.expired:
                return {
                    "success": False,
                    "error": "لا يمكن تفعيل قاعدة سعر منتهية الصلاحية.",
                }

            # Activate the rule
            rule.status = PriceStatus.active
            rule.approved_by = approved_by

            try:
                await session.commit()
            except Exception:
                # EXCLUDE constraint violation (overlapping active rules)
                await session.rollback()
                logger.exception(
                    "Failed to activate price rule | price_rule_id=%s",
                    price_rule_id,
                )
                return {
                    "success": False,
                    "error": (
                        "تعذر التفعيل: توجد قاعدة سعر نشطة متداخلة زمنياً "
                        "لنفس البند والشركة."
                    ),
                }

            logger.info(
                "Activated price rule | price_rule_id=%s | by_user=%s",
                price_rule_id,
                approved_by,
            )
            return {"success": True, "price_rule": rule}

    @staticmethod
    async def deactivate_price_rule(
        price_rule_id: int,
        supplier_id: int,
    ) -> Dict[str, Any]:
        """Deactivate (set to inactive) a price rule."""
        async with get_session(silent=True) as session:
            stmt = select(PriceRule).where(
                PriceRule.id == price_rule_id,
                PriceRule.supplier_id == supplier_id,
            )
            rule = (await session.execute(stmt)).scalar_one_or_none()

            if not rule:
                return {"success": False, "error": "قاعدة السعر غير موجودة."}

            rule.status = PriceStatus.inactive
            await session.commit()

            logger.info(
                "Deactivated price rule | price_rule_id=%s", price_rule_id
            )
            return {"success": True, "price_rule": rule}

    # ========================================================================
    # 5. PRICE TIER MANAGEMENT
    # ========================================================================

    @staticmethod
    async def add_price_tier(
        price_rule_id: int,
        supplier_id: int,
        min_qty: Decimal,
        max_qty: Optional[Decimal],
        unit_price: Decimal,
        currency: str,
    ) -> Dict[str, Any]:
        """
        Add a quantity tier to a price rule.
        The DB EXCLUDE constraint will prevent overlapping tiers.
        """
        async with get_session(silent=True) as session:
            # Validate the price rule belongs to this supplier
            stmt_rule = select(PriceRule).where(
                PriceRule.id == price_rule_id,
                PriceRule.supplier_id == supplier_id,
            )
            rule = (await session.execute(stmt_rule)).scalar_one_or_none()
            if not rule:
                return {"success": False, "error": "قاعدة السعر غير موجودة."}

            # Validate tier values
            if min_qty <= 0:
                return {
                    "success": False,
                    "error": "الحد الأدنى للكمية يجب أن يكون أكبر من صفر.",
                }
            if unit_price <= 0:
                return {
                    "success": False,
                    "error": "سعر الشريحة يجب أن يكون أكبر من صفر.",
                }
            if max_qty is not None and max_qty <= min_qty:
                return {
                    "success": False,
                    "error": "الحد الأعلى يجب أن يكون أكبر من الحد الأدنى.",
                }

            # Create the tier
            new_tier = PriceTier(
                price_rule_id=price_rule_id,
                min_qty=min_qty,
                max_qty=max_qty,
                unit_price=unit_price,
                currency=currency,
            )
            session.add(new_tier)

            try:
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to add price tier | price_rule_id=%s",
                    price_rule_id,
                )
                return {
                    "success": False,
                    "error": (
                        "تعذر إضافة الشريحة: تتداخل مع شريحة موجودة "
                        "لنفس قاعدة السعر."
                    ),
                }

            await session.refresh(new_tier)
            logger.info(
                "Added price tier | tier_id=%s | price_rule_id=%s",
                new_tier.id,
                price_rule_id,
            )
            return {"success": True, "tier": new_tier}

    @staticmethod
    async def remove_price_tier(
        tier_id: int,
        supplier_id: int,
    ) -> Dict[str, Any]:
        """Remove a price tier."""
        async with get_session(silent=True) as session:
            stmt = (
                select(PriceTier)
                .join(PriceRule, PriceTier.price_rule_id == PriceRule.id)
                .where(
                    PriceTier.id == tier_id,
                    PriceRule.supplier_id == supplier_id,
                )
            )
            tier = (await session.execute(stmt)).scalar_one_or_none()

            if not tier:
                return {"success": False, "error": "الشريحة غير موجودة."}

            await session.delete(tier)
            await session.commit()

            logger.info("Removed price tier | tier_id=%s", tier_id)
            return {"success": True}

    # ========================================================================
    # 6. GET PRICES FOR DISPLAY (Respects Isolation)
    # ========================================================================

    @staticmethod
    async def get_item_prices_for_company(
        supplier_id: int,
        item_id: int,
        company_id: int,
    ) -> Dict[str, Any]:
        """
        Get all applicable prices for a specific company viewing a
        supplier's item.
        Returns: public price + custom price (if exists for this company).
        Does NOT reveal other companies' custom prices.
        """
        async with get_session(company_id=company_id) as session:
            now = datetime.now(timezone.utc)

            # Get public price
            stmt_public = select(PriceRule).where(
                PriceRule.supplier_id == supplier_id,
                PriceRule.item_id == item_id,
                PriceRule.price_type == PriceType.public,
                PriceRule.status == PriceStatus.active,
                PriceRule.effective_from <= now,
                or_(
                    PriceRule.effective_to.is_(None),
                    PriceRule.effective_to > now,
                ),
            )
            public_rules = (
                (await session.execute(stmt_public)).scalars().all()
            )

            # Get custom price for THIS company only
            stmt_custom = select(PriceRule).where(
                PriceRule.supplier_id == supplier_id,
                PriceRule.item_id == item_id,
                PriceRule.price_type == PriceType.custom,
                PriceRule.company_id == company_id,
                PriceRule.status == PriceStatus.active,
                PriceRule.effective_from <= now,
                or_(
                    PriceRule.effective_to.is_(None),
                    PriceRule.effective_to > now,
                ),
            )
            custom_rules = (
                (await session.execute(stmt_custom)).scalars().all()
            )

            return {
                "public_price": public_rules[0] if public_rules else None,
                "custom_price": custom_rules[0] if custom_rules else None,
                "has_custom": len(custom_rules) > 0,
            }

    @staticmethod
    async def get_supplier_price_rules(
        supplier_id: int,
        item_id: Optional[int] = None,
        include_inactive: bool = False,
    ) -> List[PriceRule]:
        """
        Get all price rules for a supplier (supplier owner view).
        Supplier owner can see ALL their prices (public + all custom).
        """
        async with get_session(silent=True) as session:
            stmt = select(PriceRule).where(PriceRule.supplier_id == supplier_id)

            if item_id:
                stmt = stmt.where(PriceRule.item_id == item_id)

            if not include_inactive:
                stmt = stmt.where(
                    PriceRule.status.in_(
                        [PriceStatus.active, PriceStatus.draft]
                    )
                )

            stmt = stmt.order_by(PriceRule.created_at.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())


# Singleton instance
pricing_service = PricingService()
"""
Supplier Pricing Management Handlers
Aligned with Schema v2.0 §3.2, Permissions Matrix, Architecture v3.0 §5

Handles:
- View all active price rules (public + custom)
- Create public price rule for an item
- Create custom price rule for a specific company
- Activate / Deactivate existing price rules
- Add quantity tiers to a price rule

Permissions (from Matrix):
- Supplier Owner: Read ✅, Create ✅, Modify ✅, Approve ✅
- Supplier Manager: Read ✅, Create ✖, Modify ◐, Approve ✖
- Supplier Employee: Read ✅ only

CHANGE LOG (this revision):
- FIXED: Use PriceStatus enum (PriceStatus.active / .inactive / .draft)
  instead of raw string literals, to match the DB enum type.
- FIXED: Use Decimal for all money/quantity inputs instead of float
  (prevents floating-point precision errors).
"""
from typing import List, Optional, Any
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import sqlalchemy as sa

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session
from app.database.models.enums import PriceStatus

logger = get_logger(__name__)
router = Router(name="supplier_pricing")


# ============================================================================
# Helpers
# ============================================================================
def _esc(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def require_supplier_owner(roles: List[str]) -> bool:
    return permissions.is_supplier_owner(roles)


def require_supplier_role(roles: List[str]) -> bool:
    return permissions.is_supplier_owner(roles) or permissions.is_supplier_manager(roles)


# Predefined currency options
CURRENCY_OPTIONS = [
    ("🇷🇺 RUB", "RUB"),
    ("🇺🇸 USD", "USD"),
    ("🇪🇺 EUR", "EUR"),
    ("🇹🇷 TRY", "TRY"),
    ("🇨🇳 CNY", "CNY"),
]


# ============================================================================
# FSM: Create Price Rule
# ============================================================================
class PriceFlow(StatesGroup):
    choosing_item = State()
    choosing_type = State()
    choosing_company = State()
    entering_base_price = State()
    choosing_currency = State()
    choosing_effective_from = State()
    choosing_effective_to = State()
    confirming = State()


# ============================================================================
# FSM: Add Price Tier
# ============================================================================
class TierFlow(StatesGroup):
    entering_min_qty = State()
    entering_max_qty = State()
    entering_unit_price = State()
    confirming_tier = State()


# ============================================================================
# ENTRY POINT: 💰 إدارة الأسعار
# ⚠️ Text MUST match main_menu.py get_supplier_owner_keyboard()
# ============================================================================
@router.message(F.text == "💰 إدارة الأسعار")
async def btn_pricing_menu(message: Message, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles):
        await message.answer("⛔ غير مصرح لك بالوصول لإدارة الأسعار.")
        return
    if not supplier_id:
        await message.answer("⚠️ لم يتم العثور على حسابك كمورد نشط.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 عرض الأسعار الحالية", callback_data="prc_list")],
        [InlineKeyboardButton(text="➕ إضافة سعر عام", callback_data="prc_add_public")],
    ])

    # Only Supplier Owner can create custom prices
    if require_supplier_owner(roles):
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text="🏢 إضافة سعر مخصص لشركة", callback_data="prc_add_custom")]
        )

    await message.answer(
        "💰 <b>إدارة الأسعار</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
        "من هنا يمكنك إدارة أسعار المنتجات:\n"
        "• <b>السعر العام:</b> يظهر لجميع الشركات المرتبطة\n"
        "• <b>السعر المخصص:</b> خاص بشركة معينة فقط\n\n"
        "💡 <i>الأسعار تخضع للمراجعة والاعتماد قبل التفعيل.</i>",
        reply_markup=kb, parse_mode="HTML"
    )


# ============================================================================
# 📋 عرض الأسعار الحالية
# ============================================================================
@router.callback_query(F.data == "prc_list")
async def cb_list_prices(callback: CallbackQuery, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    try:
        from app.database.models.price_rule import PriceRule
        from app.database.models.item import Item

        async with get_session(silent=True) as session:
            stmt = (
                select(PriceRule, Item)
                .join(Item, PriceRule.item_id == Item.id)
                .where(PriceRule.supplier_id == supplier_id)
                .order_by(PriceRule.id.desc())
                .limit(20)
            )
            result = await session.execute(stmt)
            rows = result.all()

        if not rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ إضافة سعر عام", callback_data="prc_add_public")],
            ])
            await callback.message.edit_text(
                "📭 <b>لا توجد قواعد أسعار مسجلة بعد.</b>\n\nابدأ بإضافة أسعار لمنتجاتك.",
                reply_markup=kb, parse_mode="HTML"
            )
            try:
                await callback.answer()
            except Exception:
                pass
            return

        text = f"📋 <b>قواعد الأسعار ({len(rows)})</b>\n━━━━━━━━━━━━━━━━━━━\n"

        for i, (rule, item) in enumerate(rows[:15], 1):
            status_map = {
                "draft": "📝 مسودة",
                "active": "✅ نشط",
                "inactive": "⛔ معطل",
                "expired": "⏰ منتهي",
            }
            type_label = "🌐 عام" if rule.price_type == "public" else "🏢 مخصص"
            status_label = status_map.get(rule.status, rule.status)

            eff_from = rule.effective_from.strftime("%Y-%m-%d") if rule.effective_from else "-"
            eff_to = rule.effective_to.strftime("%Y-%m-%d") if rule.effective_to else "مفتوح"

            text += (
                f"\n<b>{i}. {_esc(item.name)}</b>\n"
                f"   {type_label} | {status_label}\n"
                f"   💰 {rule.base_price} {rule.currency}\n"
                f"   📅 {eff_from} → {eff_to}\n"
                f"   🆔 <code>{rule.id}</code>"
            )

        await callback.message.edit_text(text, parse_mode="HTML")

        # Send action keyboards separately for each rule that has actions
        for i, (rule, item) in enumerate(rows[:15], 1):
            action_buttons = []
            rule_status_val = rule.status.value if hasattr(rule.status, "value") else str(rule.status)
            if rule_status_val == "draft" and require_supplier_owner(roles):
                action_buttons.append(
                    InlineKeyboardButton(text="✅ تفعيل", callback_data=f"prc_activate:{rule.id}")
                )
            elif rule_status_val == "active" and require_supplier_owner(roles):
                action_buttons.append(
                    InlineKeyboardButton(text="⛔ تعطيل", callback_data=f"prc_deactivate:{rule.id}")
                )
            if require_supplier_owner(roles):
                action_buttons.append(
                    InlineKeyboardButton(text="📊 شرائح", callback_data=f"prc_tiers:{rule.id}")
                )

            if action_buttons:
                kb = InlineKeyboardMarkup(inline_keyboard=[action_buttons])
                await callback.message.answer(
                    f"#{i} <code>{rule.id}</code> — {_esc(item.name)}:",
                    reply_markup=kb, parse_mode="HTML"
                )

        try:
            await callback.answer()
        except Exception:
            pass

    except Exception:
        logger.exception("Error listing prices")
        try:
            await callback.answer("❌ حدث خطأ.", show_alert=True)
        except Exception:
            pass


# ============================================================================
# ✅ Activate Price Rule
# ============================================================================
@router.callback_query(F.data.startswith("prc_activate:"))
async def cb_activate_price(callback: CallbackQuery, user, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_owner(roles) or not supplier_id:
        await callback.answer("⛔ فقط مالك المورد يمكنه تفعيل الأسعار.", show_alert=True)
        return

    rule_id = int(callback.data.split(":", 1)[1])

    try:
        from app.database.models.price_rule import PriceRule

        async with get_session(silent=True) as session:
            stmt = select(PriceRule).where(
                PriceRule.id == rule_id,
                PriceRule.supplier_id == supplier_id
            )
            result = await session.execute(stmt)
            rule = result.scalar_one_or_none()

            if not rule:
                await callback.answer("❌ قاعدة السعر غير موجودة.", show_alert=True)
                return

            if rule.status != PriceStatus.draft:
                await callback.answer("⚠️ يمكن تفعيل المسودات فقط.", show_alert=True)
                return

            rule.status = PriceStatus.active
            rule.approved_by = user.id
            await session.commit()

        await callback.message.edit_text(
            f"✅ <b>تم تفعيل قاعدة السعر بنجاح!</b>\n\n"
            f"🆔 <code>{rule.id}</code>\n"
            f"💰 {rule.base_price} {rule.currency}\n"
            f"👤 تم الاعتماد بواسطة: {_esc(user.full_name)}",
            parse_mode="HTML"
        )
        await callback.answer("✅ تم التفعيل", show_alert=False)

    except Exception:
        logger.exception("Error activating price")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# ⛔ Deactivate Price Rule
# ============================================================================
@router.callback_query(F.data.startswith("prc_deactivate:"))
async def cb_deactivate_price(callback: CallbackQuery, user, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_owner(roles) or not supplier_id:
        await callback.answer("⛔ فقط مالك المورد يمكنه تعطيل الأسعار.", show_alert=True)
        return

    rule_id = int(callback.data.split(":", 1)[1])

    try:
        from app.database.models.price_rule import PriceRule

        async with get_session(silent=True) as session:
            stmt = select(PriceRule).where(
                PriceRule.id == rule_id,
                PriceRule.supplier_id == supplier_id
            )
            result = await session.execute(stmt)
            rule = result.scalar_one_or_none()

            if not rule:
                await callback.answer("❌ قاعدة السعر غير موجودة.", show_alert=True)
                return

            if rule.status != PriceStatus.active:
                await callback.answer("⚠️ يمكن تعطيل الأسعار النشطة فقط.", show_alert=True)
                return

            rule.status = PriceStatus.inactive
            await session.commit()

        await callback.message.edit_text(
            f"⛔ <b>تم تعطيل قاعدة السعر.</b>\n\n🆔 <code>{rule.id}</code>",
            parse_mode="HTML"
        )
        await callback.answer("⛔ تم التعطيل", show_alert=False)

    except Exception:
        logger.exception("Error deactivating price")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# ➕ إضافة سعر عام - Start FSM
# ============================================================================
@router.callback_query(F.data == "prc_add_public")
async def cb_start_public_price(callback: CallbackQuery, state: FSMContext, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_owner(roles) or not supplier_id:
        await callback.answer("⛔ فقط مالك المورد يمكنه إنشاء أسعار.", show_alert=True)
        return

    await state.update_data(supplier_id=supplier_id, price_type="public", company_id=None)

    try:
        from app.database.models.item import Item
        async with get_session(silent=True) as session:
            stmt = select(Item).where(
                Item.supplier_id == supplier_id,
                Item.is_active.is_(True)
            ).order_by(Item.name).limit(20)
            result = await session.execute(stmt)
            items = result.scalars().all()

        if not items:
            await callback.message.edit_text(
                "⚠️ <b>لا توجد أصناف نشطة في الكتالوج.</b>\n\n"
                "أضف أصنافاً أولاً من قائمة الكتالوج.",
                parse_mode="HTML"
            )
            await callback.answer()
            return

        buttons = []
        for item in items:
            buttons.append([
                InlineKeyboardButton(
                    text=f"{_esc(item.name)} ({item.sku or '-'})",
                    callback_data=f"prc_sel_item:{item.id}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")])

        await callback.message.edit_text(
            "➕ <b>إضافة سعر عام</b> (الخطوة 1/5)\n\nاختر الصنف:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.set_state(PriceFlow.choosing_item)
        await callback.answer()

    except Exception:
        logger.exception("Error loading items for pricing")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# 🏢 إضافة سعر مخصص - Start FSM
# ============================================================================
@router.callback_query(F.data == "prc_add_custom")
async def cb_start_custom_price(callback: CallbackQuery, state: FSMContext, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_owner(roles) or not supplier_id:
        await callback.answer("⛔ فقط مالك المورد يمكنه إنشاء أسعار مخصصة.", show_alert=True)
        return

    await state.update_data(supplier_id=supplier_id, price_type="custom")

    try:
        from app.database.models.item import Item
        async with get_session(silent=True) as session:
            stmt = select(Item).where(
                Item.supplier_id == supplier_id,
                Item.is_active.is_(True)
            ).order_by(Item.name).limit(20)
            result = await session.execute(stmt)
            items = result.scalars().all()

        if not items:
            await callback.message.edit_text(
                "⚠️ لا توجد أصناف نشطة. أضف أصنافاً أولاً.",
                parse_mode="HTML"
            )
            await callback.answer()
            return

        buttons = []
        for item in items:
            buttons.append([
                InlineKeyboardButton(
                    text=f"{_esc(item.name)} ({item.sku or '-'})",
                    callback_data=f"prc_sel_item:{item.id}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")])

        await callback.message.edit_text(
            "🏢 <b>إضافة سعر مخصص</b> (الخطوة 1/6)\n\nاختر الصنف:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.set_state(PriceFlow.choosing_item)
        await callback.answer()

    except Exception:
        logger.exception("Error loading items for custom pricing")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# FSM Step: Select Item
# ============================================================================
@router.callback_query(PriceFlow.choosing_item, F.data.startswith("prc_sel_item:"))
async def cb_select_item(callback: CallbackQuery, state: FSMContext):
    item_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()

    await state.update_data(item_id=item_id)

    if data.get("price_type") == "custom":
        try:
            from app.database.models.supplier_relation import SupplierRelation
            from app.database.models.company import Company

            async with get_session(silent=True) as session:
                stmt = (
                    select(SupplierRelation, Company)
                    .join(Company, SupplierRelation.company_id == Company.id)
                    .where(
                        SupplierRelation.supplier_id == data["supplier_id"],
                        SupplierRelation.status == "active"
                    )
                )
                result = await session.execute(stmt)
                relations = result.all()

            if not relations:
                await callback.message.edit_text(
                    "⚠️ لا توجد شركات مرتبطة نشطة.\n\n"
                    "اقبل طلبات ربط أولاً لإنشاء أسعار مخصصة.",
                    parse_mode="HTML"
                )
                await state.clear()
                await callback.answer()
                return

            buttons = []
            for rel, company in relations:
                buttons.append([
                    InlineKeyboardButton(
                        text=f"🏢 {_esc(company.name)}",
                        callback_data=f"prc_sel_company:{company.id}"
                    )
                ])
            buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")])

            await callback.message.edit_text(
                "🏢 <b>الخطوة 2/6:</b> اختر الشركة المستهدفة:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="HTML"
            )
            await state.set_state(PriceFlow.choosing_company)

        except Exception:
            logger.exception("Error loading companies for custom price")
            await callback.answer("❌ حدث خطأ.", show_alert=True)
    else:
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")]
        ])
        await callback.message.edit_text(
            "💰 <b>الخطوة 2/5:</b> أدخل <b>السعر الأساسي</b> للوحدة:\n(مثال: 25.50)",
            reply_markup=cancel_kb, parse_mode="HTML"
        )
        await state.set_state(PriceFlow.entering_base_price)

    await callback.answer()


# ============================================================================
# FSM Step: Select Company (Custom Price Only)
# ============================================================================
@router.callback_query(PriceFlow.choosing_company, F.data.startswith("prc_sel_company:"))
async def cb_select_company(callback: CallbackQuery, state: FSMContext):
    company_id = int(callback.data.split(":", 1)[1])
    await state.update_data(company_id=company_id)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")]
    ])
    await callback.message.edit_text(
        "💰 <b>الخطوة 3/6:</b> أدخل <b>السعر الأساسي</b> للوحدة:\n(مثال: 25.50)",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await state.set_state(PriceFlow.entering_base_price)
    await callback.answer()


# ============================================================================
# FSM Step: Base Price (text input)
# ============================================================================
@router.message(PriceFlow.entering_base_price)
async def process_base_price(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    price_text = message.text.strip().replace(",", ".") if message.text else ""
    try:
        base_price = Decimal(price_text)
        if base_price <= 0:
            await message.answer("⚠️ السعر يجب أن يكون أكبر من صفر.")
            return
    except (InvalidOperation, ValueError):
        await message.answer("⚠️ أدخل رقماً صحيحاً (مثال: 25.50)")
        return

    await state.update_data(base_price=base_price)

    buttons = []
    for label, code in CURRENCY_OPTIONS:
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"prc_currency:{code}")])
    buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")])

    await message.answer(
        "💱 <b>اختر العملة:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.set_state(PriceFlow.choosing_currency)


# ============================================================================
# FSM Step: Currency
# ============================================================================
@router.callback_query(PriceFlow.choosing_currency, F.data.startswith("prc_currency:"))
async def cb_select_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.split(":", 1)[1]
    await state.update_data(currency=currency)

    buttons = [
        [InlineKeyboardButton(text="📅 اليوم", callback_data="prc_eff_from:today")],
        [InlineKeyboardButton(text="📅 غداً", callback_data="prc_eff_from:tomorrow")],
        [InlineKeyboardButton(text="📅 بعد أسبوع", callback_data="prc_eff_from:week")],
        [InlineKeyboardButton(text="📅 بعد شهر", callback_data="prc_eff_from:month")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")],
    ]

    await callback.message.edit_text(
        "📅 <b>اختر تاريخ بدء الصلاحية:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.set_state(PriceFlow.choosing_effective_from)
    await callback.answer()


# ============================================================================
# FSM Step: Effective From
# ============================================================================
@router.callback_query(PriceFlow.choosing_effective_from, F.data.startswith("prc_eff_from:"))
async def cb_select_effective_from(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    date_map = {
        "today": today,
        "tomorrow": today + timedelta(days=1),
        "week": today + timedelta(weeks=1),
        "month": today + timedelta(days=30),
    }

    effective_from = date_map.get(choice)
    if not effective_from:
        await callback.answer("❌ خيار غير صالح.", show_alert=True)
        return

    await state.update_data(effective_from=effective_from)

    buttons = [
        [InlineKeyboardButton(text="♾️ بدون انتهاء", callback_data="prc_eff_to:none")],
        [InlineKeyboardButton(text="📅 بعد شهر", callback_data="prc_eff_to:month")],
        [InlineKeyboardButton(text="📅 بعد 3 أشهر", callback_data="prc_eff_to:3months")],
        [InlineKeyboardButton(text="📅 بعد 6 أشهر", callback_data="prc_eff_to:6months")],
        [InlineKeyboardButton(text="📅 بعد سنة", callback_data="prc_eff_to:year")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")],
    ]

    await callback.message.edit_text(
        "📅 <b>اختر تاريخ انتهاء الصلاحية:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.set_state(PriceFlow.choosing_effective_to)
    await callback.answer()


# ============================================================================
# FSM Step: Effective To
# ============================================================================
@router.callback_query(PriceFlow.choosing_effective_to, F.data.startswith("prc_eff_to:"))
async def cb_select_effective_to(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    data = await state.get_data()
    effective_from = data.get("effective_from")

    if choice == "none":
        await state.update_data(effective_to=None)
    else:
        days_map = {
            "month": 30,
            "3months": 90,
            "6months": 180,
            "year": 365,
        }
        days = days_map.get(choice)
        if not days:
            await callback.answer("❌ خيار غير صالح.", show_alert=True)
            return
        effective_to = effective_from + timedelta(days=days)
        await state.update_data(effective_to=effective_to)

    await _show_confirmation(callback.message, state)
    await state.set_state(PriceFlow.confirming)
    await callback.answer()


# ============================================================================
# Confirmation & Save
# ============================================================================
async def _show_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    eff_from = data["effective_from"].strftime("%Y-%m-%d")
    eff_to = data["effective_to"].strftime("%Y-%m-%d") if data.get("effective_to") else "مفتوح"
    type_label = "🌐 عام" if data["price_type"] == "public" else "🏢 مخصص"

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ حفظ كمسودة", callback_data="prc_confirm_save")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="prc_cancel")]
    ])

    await message.answer(
        f"📋 <b>مراجعة قاعدة السعر:</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 الصنف ID: <code>{data['item_id']}</code>\n"
        f"🏷️ النوع: {type_label}\n"
        f"💰 السعر: {data['base_price']} {data['currency']}\n"
        f"📅 من: {eff_from} → {eff_to}\n"
        f"📌 الحالة: <b>مسودة</b> (تحتاج تفعيل)\n\n"
        f"اضغط ✅ للحفظ:",
        reply_markup=confirm_kb, parse_mode="HTML"
    )


@router.callback_query(PriceFlow.confirming, F.data == "prc_confirm_save")
async def cb_confirm_save(callback: CallbackQuery, state: FSMContext, user):
    data = await state.get_data()
    await state.clear()

    try:
        from app.database.models.price_rule import PriceRule

        async with get_session(silent=True) as session:
            new_rule = PriceRule(
                supplier_id=data["supplier_id"],
                item_id=data["item_id"],
                price_type=data["price_type"],
                company_id=data.get("company_id"),
                currency=data["currency"],
                base_price=data["base_price"],
                effective_from=data["effective_from"],
                effective_to=data.get("effective_to"),
                status=PriceStatus.draft,
                version=1,
                approved_by=None,
            )
            session.add(new_rule)
            await session.commit()

        await callback.message.edit_text(
            "🎉 <b>تم حفظ قاعدة السعر كمسودة!</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 <code>{new_rule.id}</code>\n"
            f"💰 {data['base_price']} {data['currency']}\n\n"
            "⚠️ الحالة: <b>مسودة</b>\n"
            "لتفعيلها، اذهب إلى 'عرض الأسعار الحالية' واضغط ✅ تفعيل.",
            parse_mode="HTML"
        )
        await callback.answer("✅ تم الحفظ", show_alert=False)

    except Exception:
        logger.exception("Error saving price rule")
        await callback.answer("❌ حدث خطأ أثناء الحفظ.", show_alert=True)


# ============================================================================
# Cancel FSM
# ============================================================================
@router.callback_query(PriceFlow.choosing_item, F.data == "prc_cancel")
@router.callback_query(PriceFlow.choosing_company, F.data == "prc_cancel")
@router.callback_query(PriceFlow.entering_base_price, F.data == "prc_cancel")
@router.callback_query(PriceFlow.choosing_currency, F.data == "prc_cancel")
@router.callback_query(PriceFlow.choosing_effective_from, F.data == "prc_cancel")
@router.callback_query(PriceFlow.choosing_effective_to, F.data == "prc_cancel")
@router.callback_query(PriceFlow.confirming, F.data == "prc_cancel")
async def cb_cancel_price_flow(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ تم إلغاء العملية.", parse_mode="HTML")
    await callback.answer("تم الإلغاء")


# ============================================================================
# 📊 Quantity Tiers Management
# ============================================================================
@router.callback_query(F.data.startswith("prc_tiers:"))
async def cb_price_tiers(callback: CallbackQuery, roles: List[str], supplier_id: Optional[int]):
    """Display existing tiers for a price rule and offer add/delete actions."""
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    rule_id = int(callback.data.split(":", 1)[1])

    try:
        from app.database.models.price_rule import PriceRule
        from app.database.models.price_tier import PriceTier
        from app.database.models.item import Item

        async with get_session(silent=True) as session:
            rule_stmt = select(PriceRule, Item).join(Item, PriceRule.item_id == Item.id).where(
                PriceRule.id == rule_id,
                PriceRule.supplier_id == supplier_id
            )
            rule_result = await session.execute(rule_stmt)
            rule_row = rule_result.one_or_none()

            if not rule_row:
                await callback.answer("❌ قاعدة السعر غير موجودة أو لا تخصك.", show_alert=True)
                return

            rule, item = rule_row

            tier_stmt = select(PriceTier).where(
                PriceTier.price_rule_id == rule_id
            ).order_by(PriceTier.min_qty)
            tier_result = await session.execute(tier_stmt)
            tiers = tier_result.scalars().all()

        text = (
            f"📊 <b>شرائح الكميات</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 {_esc(item.name)}\n"
            f"💰 السعر الأساسي: {rule.base_price} {rule.currency}\n"
            f"🆔 قاعدة السعر: <code>{rule_id}</code>\n\n"
        )

        if tiers:
            text += "<b>الشرائح الحالية:</b>\n"
            for t in tiers:
                max_display = f"{t.max_qty}" if t.max_qty else "∞"
                text += f"   • {t.min_qty} – {max_display}: <b>{t.unit_price} {t.currency}</b> (ID: <code>{t.id}</code>)\n"
        else:
            text += "📭 <i>لا توجد شرائح كمية محددة.</i>\n"

        kb_buttons = []
        if require_supplier_owner(roles):
            kb_buttons.append([InlineKeyboardButton(text="➕ إضافة شريحة", callback_data=f"tier_add:{rule_id}")])

        if tiers and require_supplier_owner(roles):
            for t in tiers:
                max_display = f"{t.max_qty}" if t.max_qty else "∞"
                kb_buttons.append([
                    InlineKeyboardButton(
                        text=f"🗑️ حذف ({t.min_qty}–{max_display})",
                        callback_data=f"tier_del:{t.id}:{rule_id}"
                    )
                ])

        kb_buttons.append([InlineKeyboardButton(text="🔙 رجوع للأسعار", callback_data="prc_list")])

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
            parse_mode="HTML"
        )
        await callback.answer()

    except Exception:
        logger.exception("Error loading tiers")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# ➕ Add Tier - Start FSM
# ============================================================================
@router.callback_query(F.data.startswith("tier_add:"))
async def cb_start_add_tier(callback: CallbackQuery, state: FSMContext, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_owner(roles) or not supplier_id:
        await callback.answer("⛔ فقط مالك المورد يمكنه إضافة شرائح.", show_alert=True)
        return

    rule_id = int(callback.data.split(":", 1)[1])
    await state.update_data(tier_rule_id=rule_id, supplier_id=supplier_id)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="tier_cancel")]
    ])

    await callback.message.edit_text(
        "➕ <b>إضافة شريحة كمية</b> (الخطوة 1/3)\n\n"
        "أدخل <b>الحد الأدنى للكمية</b>:\n(مثال: 10)",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await state.set_state(TierFlow.entering_min_qty)
    await callback.answer()


# ============================================================================
# Tier FSM Step 1: Min Qty
# ============================================================================
@router.message(TierFlow.entering_min_qty)
async def process_tier_min_qty(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    text = message.text.strip().replace(",", ".") if message.text else ""
    try:
        min_qty = Decimal(text)
        if min_qty <= 0:
            await message.answer("⚠️ الكمية يجب أن تكون أكبر من صفر.")
            return
    except (InvalidOperation, ValueError):
        await message.answer("⚠️ أدخل رقماً صحيحاً (مثال: 10)")
        return

    await state.update_data(tier_min_qty=min_qty)

    skip_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♾️ بدون حد أعلى", callback_data="tier_skip_max")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="tier_cancel")]
    ])

    await message.answer(
        f"✅ الحد الأدنى: {min_qty}\n\n"
        "<b>الخطوة 2/3:</b> أدخل <b>الحد الأقصى للكمية</b>:\n"
        "أو اضغط '♾️ بدون حد أعلى'",
        reply_markup=skip_kb,
        parse_mode="HTML"
    )
    await state.set_state(TierFlow.entering_max_qty)


@router.callback_query(TierFlow.entering_max_qty, F.data == "tier_skip_max")
async def skip_tier_max(callback: CallbackQuery, state: FSMContext):
    await state.update_data(tier_max_qty=None)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="tier_cancel")]
    ])
    await callback.message.edit_text(
        "<b>الخطوة 3/3:</b> أدخل <b>سعر الوحدة</b> لهذه الشريحة:\n(مثال: 22.50)",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await state.set_state(TierFlow.entering_unit_price)
    await callback.answer()


# ============================================================================
# Tier FSM Step 2: Max Qty
# ============================================================================
@router.message(TierFlow.entering_max_qty)
async def process_tier_max_qty(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    text = message.text.strip().replace(",", ".") if message.text else ""
    try:
        max_qty = Decimal(text)
        if max_qty <= 0:
            await message.answer("⚠️ الكمية يجب أن تكون أكبر من صفر.")
            return
    except (InvalidOperation, ValueError):
        await message.answer("⚠️ أدخل رقماً صحيحاً (مثال: 50)")
        return

    data = await state.get_data()
    if max_qty <= data.get("tier_min_qty", 0):
        await message.answer("⚠️ الحد الأقصى يجب أن يكون أكبر من الحد الأدنى.")
        return

    await state.update_data(tier_max_qty=max_qty)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="tier_cancel")]
    ])
    await message.answer(
        f"✅ الحد الأقصى: {max_qty}\n\n"
        "<b>الخطوة 3/3:</b> أدخل <b>سعر الوحدة</b> لهذه الشريحة:\n(مثال: 22.50)",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await state.set_state(TierFlow.entering_unit_price)


# ============================================================================
# Tier FSM Step 3: Unit Price
# ============================================================================
@router.message(TierFlow.entering_unit_price)
async def process_tier_unit_price(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    text = message.text.strip().replace(",", ".") if message.text else ""
    try:
        unit_price = Decimal(text)
        if unit_price <= 0:
            await message.answer("⚠️ السعر يجب أن يكون أكبر من صفر.")
            return
    except (InvalidOperation, ValueError):
        await message.answer("⚠️ أدخل رقماً صحيحاً (مثال: 22.50)")
        return

    await state.update_data(tier_unit_price=unit_price)

    data = await state.get_data()
    max_display = f"{data['tier_max_qty']}" if data.get("tier_max_qty") else "∞"

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ حفظ الشريحة", callback_data="tier_confirm_save")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="tier_cancel")]
    ])

    await message.answer(
        f"📋 <b>مراجعة الشريحة:</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 من: {data['tier_min_qty']} → إلى: {max_display}\n"
        f"💰 سعر الوحدة: {unit_price}\n\n"
        f"اضغط ✅ للحفظ:",
        reply_markup=confirm_kb,
        parse_mode="HTML"
    )
    await state.set_state(TierFlow.confirming_tier)


# ============================================================================
# Tier: Confirm & Save
# ============================================================================
@router.callback_query(TierFlow.confirming_tier, F.data == "tier_confirm_save")
async def cb_confirm_tier_save(callback: CallbackQuery, state: FSMContext, user):
    data = await state.get_data()
    await state.clear()

    rule_id = data.get("tier_rule_id")
    supplier_id = data.get("supplier_id")

    try:
        from app.database.models.price_rule import PriceRule
        from app.database.models.price_tier import PriceTier

        async with get_session(silent=True) as session:
            rule = await session.get(PriceRule, rule_id)
            if not rule or rule.supplier_id != supplier_id:
                await callback.answer("❌ قاعدة السعر غير موجودة أو لا تخصك.", show_alert=True)
                return

            new_tier = PriceTier(
                price_rule_id=rule_id,
                min_qty=data["tier_min_qty"],
                max_qty=data.get("tier_max_qty"),
                unit_price=data["tier_unit_price"],
                currency=rule.currency,
            )
            session.add(new_tier)
            await session.commit()

        max_display = f"{data['tier_max_qty']}" if data.get("tier_max_qty") else "∞"

        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 رجوع للشرائح", callback_data=f"prc_tiers:{rule_id}")]
        ])

        await callback.message.edit_text(
            f"🎉 <b>تم حفظ الشريحة بنجاح!</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 من: {data['tier_min_qty']} → إلى: {max_display}\n"
            f"💰 سعر الوحدة: {data['tier_unit_price']} {rule.currency}\n"
            f"🆔 <code>{new_tier.id}</code>",
            reply_markup=back_kb,
            parse_mode="HTML"
        )
        await callback.answer("✅ تم الحفظ", show_alert=False)

    except Exception as e:
        logger.exception("Error saving tier")
        error_msg = str(e)
        if "exclude" in error_msg.lower() or "overlap" in error_msg.lower() or "conflict" in error_msg.lower():
            await callback.message.answer(
                "⚠️ <b>تداخل في الشرائح!</b>\n\n"
                "هذه الشريحة تتداخل مع شريحة موجودة.\n"
                "الرجاء اختيار نطاق مختلف.",
                parse_mode="HTML"
            )
        else:
            await callback.answer("❌ حدث خطأ أثناء الحفظ.", show_alert=True)


# ============================================================================
# 🗑️ Delete Tier
# ============================================================================
@router.callback_query(F.data.startswith("tier_del:"))
async def cb_delete_tier(callback: CallbackQuery, user, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_owner(roles) or not supplier_id:
        await callback.answer("⛔ فقط مالك المورد يمكنه حذف شرائح.", show_alert=True)
        return

    parts = callback.data.split(":")
    tier_id = int(parts[1])
    rule_id = int(parts[2])

    try:
        from app.database.models.price_tier import PriceTier
        from app.database.models.price_rule import PriceRule

        async with get_session(silent=True) as session:
            rule = await session.get(PriceRule, rule_id)
            if not rule or rule.supplier_id != supplier_id:
                await callback.answer("❌ قاعدة السعر غير موجودة أو لا تخصك.", show_alert=True)
                return

            tier = await session.get(PriceTier, tier_id)
            if not tier or tier.price_rule_id != rule_id:
                await callback.answer("❌ الشريحة غير موجودة.", show_alert=True)
                return

            await session.delete(tier)
            await session.commit()

        await callback.answer("✅ تم حذف الشريحة", show_alert=False)

        # Refresh tier list
        await cb_price_tiers(callback, roles, supplier_id)

    except Exception:
        logger.exception("Error deleting tier")
        await callback.answer("❌ حدث خطأ أثناء الحذف.", show_alert=True)


# ============================================================================
# Cancel Tier FSM
# ============================================================================
@router.callback_query(TierFlow.entering_min_qty, F.data == "tier_cancel")
@router.callback_query(TierFlow.entering_max_qty, F.data == "tier_cancel")
@router.callback_query(TierFlow.entering_unit_price, F.data == "tier_cancel")
@router.callback_query(TierFlow.confirming_tier, F.data == "tier_cancel")
async def cb_cancel_tier_flow(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rule_id = data.get("tier_rule_id")
    await state.clear()

    if rule_id:
        await callback.message.edit_text("❌ تم إلغاء إضافة الشريحة.", parse_mode="HTML")
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 رجوع للشرائح", callback_data=f"prc_tiers:{rule_id}")]
        ])
        await callback.message.answer("اختر إجراءً:", reply_markup=back_kb, parse_mode="HTML")
    else:
        await callback.message.edit_text("❌ تم إلغاء العملية.", parse_mode="HTML")

    await callback.answer("تم الإلغاء")
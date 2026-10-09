"""
Supplier Panel handlers - Dashboard and actions for supplier owners/managers.
Aligned with Procurement Bot Architecture v3.0, Schema v2.0, and Permissions Matrix.

Handles Supplier Dashboard entry point and all sub-actions:
- 📨 طلبات الربط (Link Requests)
- 📦 الكتالوج (Catalog Management)
- 📊 تقاريري (Reports)
- 🔄 تحديث بياناتي (Update Info)

IMPROVEMENTS (this revision):
- Report button text renamed to "📊 تقاريري" to match the updated
  main_menu.py keyboard and avoid collision with company_owner's
  "📊 التقارير" handler.
- ADDED: Backward-compat alias "📊 التقارير" and "📊 تقرير المورد"
  (old button texts that may still be cached in users' Telegram clients).
  All three now route to the same reports handler.
- ADDED: Excel export for supplier reports (items + relations sheets).
- ADDED: Update Info feature (moved from supplier_registration.py to
  consolidate dashboard features in this file).
- Update Info uses audit_service.log_event (the previous call used
  audit_service.log which does not exist and silently failed).
"""
from typing import List, Optional, Any
from datetime import datetime, timezone
from io import BytesIO
import sqlalchemy as sa

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from sqlalchemy import select, func

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session
from app.database.models.unit_of_measure import UnitOfMeasure
from app.database.models.enums import AuditAction
from app.services.catalog_service import catalog_service

logger = get_logger(__name__)
router = Router(name="supplier_panel")


# Email regex (used in Update Info)
EMAIL_REGEX = __import__("re").compile(
    r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
)


# ============================================================================
# Helper: Permission & Safety Checks
# ============================================================================
def require_supplier_role(roles: List[str]) -> bool:
    """Check if user has any supplier role (Owner or Manager)."""
    return permissions.is_supplier_owner(roles) or permissions.is_supplier_manager(roles)


def _esc(value: Any) -> str:
    """Escape user input for HTML parse mode safely."""
    if value is None:
        return ""
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _get_supplier_keyboard() -> InlineKeyboardMarkup:
    """Generate the main Supplier Dashboard keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📨 طلبات الربط", callback_data="sp_link_requests"),
            InlineKeyboardButton(text="📦 الكتالوج", callback_data="sp_catalog_menu")
        ],
        [
            InlineKeyboardButton(text="📊 التقارير", callback_data="sp_reports"),
            InlineKeyboardButton(text="🔄 تحديث بياناتي", callback_data="sp_update_info")
        ],
        [InlineKeyboardButton(text="🔙 رجوع للقائمة الرئيسية", callback_data="go_to_main_menu")]
    ])


# ============================================================================
# FSM States for Catalog Management
# ============================================================================
class CatalogFlow(StatesGroup):
    waiting_for_item_name = State()
    waiting_for_item_sku = State()
    waiting_for_item_desc = State()
    waiting_for_item_uom = State()
    waiting_for_item_uom_custom = State()


# ============================================================================
# FSM States for Update Info
# ============================================================================
class SupplierUpdateFlow(StatesGroup):
    waiting_for_field_choice = State()
    waiting_for_new_value = State()


# ============================================================================
# Sub-handlers for ReplyKeyboard routing
# ============================================================================
async def _handle_link_requests(message: Message, roles: List[str], supplier_id: int):
    """Handle '📨 طلبات الربط' button press."""
    try:
        from app.database.models.supplier_relation import SupplierRelation
        from app.database.models.company import Company

        async with get_session(silent=True) as session:
            stmt = (
                select(SupplierRelation, Company)
                .join(Company, SupplierRelation.company_id == Company.id)
                .where(
                    SupplierRelation.supplier_id == supplier_id,
                    SupplierRelation.status == "pending"
                )
                .order_by(SupplierRelation.id.desc())
            )
            result = await session.execute(stmt)
            relations = result.all()

        if not relations:
            await message.answer(
                "📭 <b>لا توجد طلبات ربط معلقة حالياً.</b>",
                parse_mode="HTML"
            )
            return

        await message.answer(
            f"📨 <b>طلبات الربط المعلقة ({len(relations)} طلب):</b>",
            parse_mode="HTML"
        )

        for i, (rel, company) in enumerate(relations[:10], 1):
            date_str = rel.created_at.strftime("%Y-%m-%d %H:%M") if rel.created_at else "N/A"
            card_text = (
                f"\n<b>{i}. 🏢 {_esc(company.name)}</b>\n"
                f"   🏙️ المدينة: {company.city or 'غير محدد'}\n"
                f"   📅 تاريخ الطلب: {date_str}\n"
                f"   🆔 رقم الشركة: <code>{company.id}</code>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ قبول", callback_data=f"sp_accept_link:{rel.id}"),
                    InlineKeyboardButton(text="❌ رفض", callback_data=f"sp_reject_link:{rel.id}")
                ]
            ])
            await message.answer(card_text, reply_markup=keyboard, parse_mode="HTML")

    except Exception:
        logger.exception("Error fetching link requests")
        await message.answer("❌ حدث خطأ أثناء جلب طلبات الربط.")


async def _handle_catalog_menu(message: Message, roles: List[str], supplier_id: int):
    """Handle '📦 الكتالوج' button press - show catalog options."""
    catalog_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ إضافة صنف جديد", callback_data="cat_add_item"),
            InlineKeyboardButton(text="📋 عرض الأصناف", callback_data="cat_list_items")
        ],
    ])
    await message.answer(
        "📦 <b>إدارة كتالوج الأصناف</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
        "الأصناف النشطة ستظهر للشركات المشترية عند دعوتهم لعروض الأسعار.",
        reply_markup=catalog_keyboard,
        parse_mode="HTML"
    )


async def _handle_reports(message: Message, roles: List[str], supplier_id: int):
    """Handle '📊 تقاريري' button press."""
    try:
        from app.database.models.supplier_relation import SupplierRelation
        from app.database.models.item import Item

        async with get_session(silent=True) as session:
            rel_stats = await session.execute(
                select(SupplierRelation.status, func.count(SupplierRelation.id))
                .where(SupplierRelation.supplier_id == supplier_id)
                .group_by(SupplierRelation.status)
            )
            item_stats = await session.execute(
                select(func.count(Item.id), func.sum(func.cast(Item.is_active, sa.Integer)))
                .where(Item.supplier_id == supplier_id)
            )
            total_items, active_items = item_stats.one()

        response = "📊 <b>تقرير المورد المختصر</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
        response += f"📦 الأصناف: {active_items or 0} نشط / {total_items or 0} إجمالي\n\n🏪 العلاقات:\n"

        has_relations = False
        for status, count in rel_stats.all():
            labels = {"pending": "⏳ معلق", "active": "✅ نشط", "rejected": "❌ مرفوض", "suspended": "⛔ موقوف"}
            response += f"   • {labels.get(status, status)}: {count}\n"
            has_relations = True
        if not has_relations:
            response += "   • لا توجد علاقات مسجلة\n"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 تصدير إلى Excel", callback_data="sp_export_report_excel")]
        ])
        await message.answer(response, reply_markup=kb, parse_mode="HTML")

    except Exception:
        logger.exception("Report error")
        await message.answer("❌ فشل إنشاء التقرير.")


async def _handle_update_info(message: Message, roles: List[str], supplier_id: Optional[int]):
    """Handle '🔄 تحديث بياناتي' button press."""
    if not permissions.is_supplier_owner(roles):
        await message.answer("⛔ فقط مالك المورد يمكنه تحديث البيانات.")
        return

    if not supplier_id:
        await message.answer("⚠️ لم يتم العثور على حسابك كمورد.")
        return

    try:
        from app.database.models.supplier import Supplier

        async with get_session(silent=True) as session:
            supplier = await session.get(Supplier, supplier_id)
            if not supplier:
                await message.answer("❌ لم يتم العثور على بيانات المورد.")
                return

        cities_display = ", ".join(supplier.cities) if supplier.cities else "غير محدد"

        info_text = (
            "🔄 <b>تحديث بيانات المورد</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏢 الاسم: <b>{_esc(supplier.name)}</b>\n"
            f"🏭 النشاط: {_esc(supplier.activity or 'غير محدد')}\n"
            f"🏙️ المدن: {_esc(cities_display)}\n"
            f"📧 البريد: {_esc(supplier.contact_email or 'غير محدد')}\n\n"
            "اختر الحقل الذي تريد تعديله:"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏢 تعديل الاسم", callback_data="upd_field:name")],
            [InlineKeyboardButton(text="🏭 تعديل النشاط", callback_data="upd_field:activity")],
            [InlineKeyboardButton(text="🏙️ تعديل المدن", callback_data="upd_field:cities")],
            [InlineKeyboardButton(text="📧 تعديل البريد", callback_data="upd_field:email")],
        ])

        await message.answer(info_text, reply_markup=kb, parse_mode="HTML")

    except Exception:
        logger.exception("Error loading supplier info for update")
        await message.answer("❌ حدث خطأ أثناء تحميل البيانات.")


# ============================================================================
# ENTRY POINT: Supplier Dashboard Keyboard
# ============================================================================
@router.message(F.text.in_([
    "📨 طلبات الربط",
    "📦 الكتالوج",
    "📊 تقاريري",
    "📊 التقارير",
    "📊 تقرير المورد",  # ← backward compat alias
    "🔄 تحديث بياناتي",
]))
async def btn_supplier_dashboard(message: Message, roles: List[str], supplier_id: Optional[int]):
    """
    Catch-all for supplier panel buttons.
    Routes to the appropriate sub-handler based on exact text match.
    """
    if not require_supplier_role(roles):
        await message.answer("⛔ غير مصرح لك بالوصول للوحة التحكم.")
        return

    if not supplier_id:
        await message.answer("⚠️ لم يتم العثور على حسابك كمورد نشط.")
        return

    text = message.text.strip() if message.text else ""

    # Route to specific handlers based on button text
    if text == "📨 طلبات الربط":
        await _handle_link_requests(message, roles, supplier_id)
    elif text == "📦 الكتالوج":
        await _handle_catalog_menu(message, roles, supplier_id)
    elif text in ("📊 تقاريري", "📊 التقارير", "📊 تقرير المورد"):
        await _handle_reports(message, roles, supplier_id)
    elif text == "🔄 تحديث بياناتي":
        await _handle_update_info(message, roles, supplier_id)
    else:
        # Fallback: show dashboard keyboard
        from app.bot.keyboards.main_menu import get_supplier_owner_keyboard
        await message.answer(
            "🏢 <b>لوحة تحكم المورد</b>\n━━━━━━━━━━━━━━━━━━━\n\nاختر إجراءً من القائمة:",
            reply_markup=get_supplier_owner_keyboard(),
            parse_mode="HTML"
        )


# ============================================================================
# SECTION: Link Requests (📨 طلبات الربط)
# ============================================================================
@router.callback_query(F.data == "sp_link_requests")
async def cb_link_requests(callback: CallbackQuery, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    try:
        from app.database.models.supplier_relation import SupplierRelation
        from app.database.models.company import Company

        async with get_session(silent=True) as session:
            stmt = (
                select(SupplierRelation, Company)
                .join(Company, SupplierRelation.company_id == Company.id)
                .where(
                    SupplierRelation.supplier_id == supplier_id,
                    SupplierRelation.status == "pending"
                )
                .order_by(SupplierRelation.id.desc())
            )
            result = await session.execute(stmt)
            relations = result.all()

        if not relations:
            back_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="open_supplier_panel")]
            ])
            await callback.message.edit_text(
                "📭 <b>لا توجد طلبات ربط معلقة حالياً.</b>\n\n"
                "ستظهر هنا طلبات الربط الجديدة من الشركات المشترية.",
                reply_markup=back_kb,
                parse_mode="HTML"
            )
            await callback.answer()
            return

        # Show first pending request with actions
        rel, company = relations[0]
        date_str = rel.created_at.strftime("%Y-%m-%d %H:%M") if rel.created_at else "N/A"

        card_text = (
            f"📨 <b>طلبات الربط المعلقة ({len(relations)} طلب)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>🏢 {company.name}</b>\n"
            f"🏙️ المدينة: {company.city or 'غير محدد'}\n"
            f"📅 تاريخ الطلب: {date_str}\n"
            f"🆔 رقم الشركة: <code>{company.id}</code>"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ قبول", callback_data=f"sp_accept_link:{rel.id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"sp_reject_link:{rel.id}")
            ],
            [InlineKeyboardButton(text="🔙 رجوع للقائمة", callback_data="open_supplier_panel")]
        ])

        await callback.message.edit_text(card_text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()

    except Exception:
        logger.exception("Error fetching link requests")
        await callback.answer("❌ حدث خطأ أثناء جلب الطلبات.", show_alert=True)


@router.callback_query(F.data.startswith("sp_accept_link:"))
async def cb_accept_link(callback: CallbackQuery, user, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    relation_id = int(callback.data.split(":", 1)[1])

    try:
        from app.database.models.supplier_relation import SupplierRelation

        async with get_session(silent=True) as session:
            stmt = select(SupplierRelation).where(
                SupplierRelation.id == relation_id,
                SupplierRelation.supplier_id == supplier_id
            )
            result = await session.execute(stmt)
            relation = result.scalar_one_or_none()

            if not relation:
                await callback.answer("❌ طلب الربط غير موجود أو لا يخصك.", show_alert=True)
                return

            relation.status = "active"
            relation.decided_by = user.id
            relation.decided_at = datetime.now(timezone.utc)
            await session.commit()

        await callback.message.edit_text(
            "✅ <b>تم قبول طلب الربط بنجاح!</b>\n\n"
            "يمكنك الآن استقبال طلبات الشراء والعروض من هذه الشركة.",
            parse_mode="HTML"
        )
        await callback.answer("✅ تم القبول", show_alert=False)

    except Exception:
        logger.exception("Error accepting link")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


@router.callback_query(F.data.startswith("sp_reject_link:"))
async def cb_reject_link(callback: CallbackQuery, user, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    relation_id = int(callback.data.split(":", 1)[1])

    try:
        from app.database.models.supplier_relation import SupplierRelation

        async with get_session(silent=True) as session:
            stmt = select(SupplierRelation).where(
                SupplierRelation.id == relation_id,
                SupplierRelation.supplier_id == supplier_id
            )
            result = await session.execute(stmt)
            relation = result.scalar_one_or_none()

            if not relation:
                await callback.answer("❌ طلب الربط غير موجود.", show_alert=True)
                return

            relation.status = "rejected"
            relation.decided_by = user.id
            relation.decided_at = datetime.now(timezone.utc)
            relation.status_reason = "مرفوض من قبل المورد"
            await session.commit()

        await callback.message.edit_text(
            "❌ <b>تم رفض طلب الربط.</b>",
            parse_mode="HTML"
        )
        await callback.answer("❌ تم الرفض", show_alert=False)

    except Exception:
        logger.exception("Error rejecting link")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# SECTION: Catalog Management (📦 الكتالوج)
# ============================================================================
@router.callback_query(F.data == "sp_catalog_menu")
async def cb_catalog_menu(callback: CallbackQuery, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ إضافة صنف", callback_data="cat_add_item"),
            InlineKeyboardButton(text="📋 عرض الأصناف", callback_data="cat_list_items")
        ],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="open_supplier_panel")]
    ])

    await callback.message.edit_text(
        "📦 <b>إدارة كتالوج الأصناف</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "الأصناف النشطة ستظهر للشركات المشترية عند دعوتهم لعروض الأسعار.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cat_add_item")
async def cb_start_add_item(callback: CallbackQuery, state: FSMContext, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    await state.update_data(supplier_id=supplier_id)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="cat_cancel_flow")]
    ])

    await callback.message.edit_text(
        "➕ <b>إضافة صنف جديد</b> (الخطوة 1/4)\n\n"
        "ما هو <b>اسم الصنف</b> أو المنتج؟\n"
        "(مثال: ورق طباعة A4)",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await state.set_state(CatalogFlow.waiting_for_item_name)
    await callback.answer()


@router.callback_query(CatalogFlow.waiting_for_item_name, F.data == "cat_cancel_flow")
@router.callback_query(CatalogFlow.waiting_for_item_sku, F.data == "cat_cancel_flow")
@router.callback_query(CatalogFlow.waiting_for_item_desc, F.data == "cat_cancel_flow")
@router.callback_query(CatalogFlow.waiting_for_item_uom, F.data == "cat_cancel_flow")
@router.callback_query(CatalogFlow.waiting_for_item_uom_custom, F.data == "cat_cancel_flow")
async def cb_cancel_catalog_flow(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ تم إلغاء العملية.", parse_mode="HTML")
    await callback.answer("تم الإلغاء")


@router.message(CatalogFlow.waiting_for_item_name)
async def process_item_name(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    item_name = message.text.strip() if message.text else ""
    if len(item_name) < 3:
        await message.answer("⚠️ اسم الصنف قصير جداً (3 أحرف على الأقل).")
        return

    await state.update_data(item_name=item_name)

    skip_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ تخطي SKU", callback_data="cat_skip_sku")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="cat_cancel_flow")]
    ])

    await message.answer(
        f"✅ الاسم: {_esc(item_name)}\n\n"
        f"<b>الخطوة 2/4:</b> ما هو رمز الصنف (SKU)؟ (اختياري)",
        reply_markup=skip_kb,
        parse_mode="HTML"
    )
    await state.set_state(CatalogFlow.waiting_for_item_sku)


@router.callback_query(CatalogFlow.waiting_for_item_sku, F.data == "cat_skip_sku")
async def skip_sku(callback: CallbackQuery, state: FSMContext):
    await state.update_data(item_sku=None)
    await _ask_description(callback.message)
    await state.set_state(CatalogFlow.waiting_for_item_desc)
    await callback.answer()


@router.message(CatalogFlow.waiting_for_item_sku)
async def process_sku(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    sku = message.text.strip() if message.text else ""
    if len(sku) > 200:
        await message.answer("⚠️ SKU طويل جداً (200 حرف كحد أقصى).")
        return

    await state.update_data(item_sku=sku)
    await _ask_description(message)
    await state.set_state(CatalogFlow.waiting_for_item_desc)


async def _ask_description(message: Message):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="cat_cancel_flow")]
    ])
    await message.answer(
        "<b>الخطوة 3/4:</b> أدخل الوصف أو المواصفات الفنية:",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )


@router.message(CatalogFlow.waiting_for_item_desc)
async def process_desc(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    desc = message.text.strip() if message.text else ""
    await state.update_data(item_desc=desc)

    # Fetch UOMs
    try:
        uoms = await catalog_service.get_all_units_of_measure()
        if not uoms:
            await message.answer("⚠️ لا توجد وحدات قياس معرفة في النظام. تواصل مع الإدارة.")
            await state.clear()
            return

        buttons = []
        row = []
        for uom in uoms:
            row.append(InlineKeyboardButton(text=uom.code, callback_data=f"cat_uom:{uom.id}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        buttons.append([InlineKeyboardButton(text="✏️ وحدة أخرى", callback_data="cat_uom_custom")])
        buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="cat_cancel_flow")])

        await message.answer(
            "<b>الخطوة 4/4:</b> اختر وحدة القياس:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.set_state(CatalogFlow.waiting_for_item_uom)
    except Exception:
        logger.exception("Error fetching UOMs")
        await message.answer("❌ خطأ في جلب وحدات القياس.")
        await state.clear()


@router.callback_query(CatalogFlow.waiting_for_item_uom, F.data.startswith("cat_uom:"))
async def process_uom_selection(callback: CallbackQuery, state: FSMContext):
    uom_id = int(callback.data.split(":", 1)[1])
    await state.update_data(item_uom_id=uom_id)
    await _finalize_add_item(callback.message, state)
    await callback.answer()


@router.callback_query(CatalogFlow.waiting_for_item_uom, F.data == "cat_uom_custom")
async def prompt_custom_uom(callback: CallbackQuery, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="cat_cancel_flow")]
    ])
    await callback.message.edit_text(
        "✏️ اكتب كود وحدة القياس (بالإنجليزية):\n(مثال: pcs, kg, m, box)",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await state.set_state(CatalogFlow.waiting_for_item_uom_custom)
    await callback.answer()


@router.message(CatalogFlow.waiting_for_item_uom_custom)
async def process_custom_uom(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم الإلغاء.")
        return

    uom_code = message.text.strip().lower() if message.text else ""
    if len(uom_code) < 2 or len(uom_code) > 20:
        await message.answer("⚠️ كود الوحدة غير صالح.")
        return

    async with get_session(silent=True) as session:
        stmt = select(UnitOfMeasure).where(UnitOfMeasure.code == uom_code)
        result = await session.execute(stmt)
        uom = result.scalar_one_or_none()

        if not uom:
            await message.answer(
                f"⚠️ الوحدة '{_esc(uom_code)}' غير موجودة في النظام.\n"
                "الرجاء اختيار وحدة من القائمة أو إدخال كود صحيح."
            )
            return

        await state.update_data(item_uom_id=uom.id)

    await _finalize_add_item(message, state)


async def _finalize_add_item(message: Message, state: FSMContext):
    data = await state.get_data()
    supplier_id = data.get("supplier_id")
    await state.clear()

    if not supplier_id:
        await message.answer("❌ خطأ: فقدت جلسة المورد.")
        return

    attributes = {}
    if data.get("item_desc"):
        attributes["description"] = data["item_desc"]

    result = await catalog_service.create_item(
        supplier_id=supplier_id,
        name=data["item_name"],
        uom_id=data["item_uom_id"],
        category_id=None,
        sku=data.get("item_sku"),
        attributes=attributes
    )

    if not result["success"]:
        await message.answer(f"❌ {result['error']}")
        return

    new_item = result["item"]
    uom_display = "وحدة"
    try:
        async with get_session(silent=True) as s:
            uom_obj = await s.get(UnitOfMeasure, data["item_uom_id"])
            if uom_obj:
                uom_display = uom_obj.code
    except Exception:
        pass

    success_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ إضافة آخر", callback_data="cat_add_item"),
            InlineKeyboardButton(text="📋 عرض القائمة", callback_data="cat_list_items")
        ],
        [InlineKeyboardButton(text="🔙 لوحة التحكم", callback_data="open_supplier_panel")]
    ])

    await message.answer(
        "🎉 <b>تم حفظ الصنف بنجاح!</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 {_esc(new_item.name)}\n"
        f"🏷️ SKU: {_esc(new_item.sku or '-')}\n"
        f"📏 الوحدة: {uom_display}\n\n"
        "💡 <i>لتعيين سعر لهذا الصنف، استخدم قائمة إدارة الأسعار.</i>",
        reply_markup=success_kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data == "cat_list_items")
async def cb_list_items(callback: CallbackQuery, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    try:
        result = await catalog_service.list_supplier_items(
            supplier_id=supplier_id,
            viewer_supplier_id=supplier_id,
            include_inactive=True,
            limit=20
        )
        items = result.get("items", [])

        if not items:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ إضافة صنف", callback_data="cat_add_item")],
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="sp_catalog_menu")]
            ])
            await callback.message.edit_text(
                "📭 لا توجد أصناف في كتالوجك بعد.",
                reply_markup=kb,
                parse_mode="HTML"
            )
            await callback.answer()
            return

        text = f"📋 <b>أصناف الكتالوج ({len(items)})</b>\n━━━━━━━━━━━━━━━━━━━\n"

        for i, item in enumerate(items[:10], 1):
            status = "✅" if item.is_active else "⛔"
            text += f"\n{i}. {status} <b>{_esc(item.name)}</b>\n"
            text += f"   SKU: {_esc(item.sku or '-')} | ID: <code>{item.id}</code>\n"

        if len(items) > 10:
            text += f"\n<i>... و {len(items)-10} أصناف أخرى</i>"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ إضافة صنف", callback_data="cat_add_item")],
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="sp_catalog_menu")]
        ])

        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    except Exception:
        logger.exception("Error listing items")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# SECTION: Reports & Update Info
# ============================================================================
@router.callback_query(F.data == "sp_reports")
async def cb_reports(callback: CallbackQuery, roles: List[str], supplier_id: Optional[int]):
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    try:
        from app.database.models.supplier_relation import SupplierRelation
        from app.database.models.item import Item

        async with get_session(silent=True) as session:
            rel_stats = await session.execute(
                select(SupplierRelation.status, func.count(SupplierRelation.id))
                .where(SupplierRelation.supplier_id == supplier_id)
                .group_by(SupplierRelation.status)
            )

            item_stats = await session.execute(
                select(func.count(Item.id), func.sum(func.cast(Item.is_active, sa.Integer)))
                .where(Item.supplier_id == supplier_id)
            )
            total_items, active_items = item_stats.one()

        response = "📊 <b>تقرير المورد المختصر</b>\n━━━━━━━━━━━━━━━━━━━\n\n"
        response += f"📦 الأصناف: {active_items or 0} نشط / {total_items or 0} إجمالي\n\n"
        response += "🏪 العلاقات:\n"

        has_relations = False
        for status, count in rel_stats.all():
            labels = {"pending": "⏳ معلق", "active": "✅ نشط", "rejected": "❌ مرفوض", "suspended": "⛔ موقوف"}
            response += f"   • {labels.get(status, status)}: {count}\n"
            has_relations = True

        if not has_relations:
            response += "   • لا توجد علاقات مسجلة\n"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 تصدير إلى Excel", callback_data="sp_export_report_excel")],
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="open_supplier_panel")]
        ])

        await callback.message.edit_text(response, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    except Exception:
        logger.exception("Report error")
        await callback.answer("❌ فشل إنشاء التقرير.", show_alert=True)


# ============================================================================
# Callback: Export Supplier Report to Excel
# ============================================================================
@router.callback_query(F.data == "sp_export_report_excel")
async def cb_export_supplier_report(
    callback: CallbackQuery,
    roles: List[str],
    supplier_id: Optional[int],
):
    """Generate an Excel file for the supplier report (items + relations)."""
    if not require_supplier_role(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    try:
        from app.database.models.supplier import Supplier
        from app.database.models.supplier_relation import SupplierRelation
        from app.database.models.company import Company
        from app.database.models.item import Item
        from app.database.models.unit_of_measure import UnitOfMeasure
        import openpyxl

        async with get_session(silent=True) as session:
            supplier = await session.get(Supplier, supplier_id)

            items_stmt = (
                select(Item, UnitOfMeasure.code)
                .outerjoin(UnitOfMeasure, Item.uom_id == UnitOfMeasure.id)
                .where(Item.supplier_id == supplier_id)
                .order_by(Item.id.asc())
            )
            items_result = await session.execute(items_stmt)
            items_rows = items_result.all()

            rel_stmt = (
                select(SupplierRelation, Company)
                .outerjoin(Company, SupplierRelation.company_id == Company.id)
                .where(SupplierRelation.supplier_id == supplier_id)
                .order_by(SupplierRelation.id.desc())
            )
            rel_result = await session.execute(rel_stmt)
            rel_rows = rel_result.all()

        if not supplier:
            await callback.answer("❌ لم يتم العثور على بيانات المورد.", show_alert=True)
            return

        wb = openpyxl.Workbook()

        # Sheet 1: Supplier Info
        ws_info = wb.active
        ws_info.title = "بيانات المورد"
        ws_info.append(["الحقل", "القيمة"])
        ws_info.append(["الاسم", supplier.name])
        ws_info.append(["الرقم الضريبي (INN)", supplier.inn or "-"])
        ws_info.append(["النشاط", supplier.activity or "-"])
        ws_info.append(["المدن", ", ".join(supplier.cities) if supplier.cities else "-"])
        ws_info.append(["البريد الإلكتروني", supplier.contact_email or "-"])
        status_val = supplier.status.value if hasattr(supplier.status, "value") else str(supplier.status)
        ws_info.append(["الحالة", status_val])
        ws_info.append(["مُوثَّق", "نعم" if supplier.is_verified else "لا"])
        ws_info.append(["تاريخ التقرير", datetime.now().strftime("%Y-%m-%d %H:%M")])

        # Sheet 2: Items
        ws_items = wb.create_sheet("الأصناف")
        ws_items.append(["#", "اسم الصنف", "SKU", "الوحدة", "الحالة"])
        for i, (item, uom_code) in enumerate(items_rows, 1):
            ws_items.append([
                i,
                item.name,
                item.sku or "-",
                uom_code or "-",
                "نشط" if item.is_active else "معطّل",
            ])

        # Sheet 3: Relations
        ws_rel = wb.create_sheet("العلاقات")
        ws_rel.append(["#", "الشركة", "المدينة", "الحالة", "تاريخ القرار"])
        for i, (rel, company) in enumerate(rel_rows, 1):
            rel_status = rel.status.value if hasattr(rel.status, "value") else str(rel.status)
            decided = rel.decided_at.strftime("%Y-%m-%d %H:%M") if rel.decided_at else "-"
            ws_rel.append([
                i,
                company.name if company else f"شركة #{rel.company_id}",
                (company.city if company and company.city else "-"),
                rel_status,
                decided,
            ])

        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        filename = f"supplier_report_{supplier_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        doc = BufferedInputFile(buffer.read(), filename=filename)

        await callback.message.answer_document(
            doc,
            caption="📥 <b>تقرير المورد (Excel)</b>\nيتضمن: بيانات المورد، الأصناف، العلاقات.",
            parse_mode="HTML",
        )
        await callback.answer("✅ تم التصدير بنجاح", show_alert=False)

    except ImportError:
        logger.error("openpyxl not installed.")
        await callback.answer("❌ مكتبة Excel غير مثبتة على الخادم.", show_alert=True)
    except Exception:
        logger.exception("Supplier Excel export error")
        await callback.answer("❌ فشل التصدير. حاول لاحقاً.", show_alert=True)


@router.callback_query(F.data == "sp_update_info")
async def cb_update_info(callback: CallbackQuery, roles: List[str], supplier_id: Optional[int]):
    """Entry point for update-info from inline keyboard (equivalent to _handle_update_info)."""
    await _handle_update_info(callback.message, roles, supplier_id)
    await callback.answer()


# ============================================================================
# SECTION: Update Info — FSM Handlers
# ============================================================================
@router.callback_query(F.data.startswith("upd_field:"))
async def cb_upd_field_choice(callback: CallbackQuery, state: FSMContext, roles: List[str], supplier_id: Optional[int]):
    if not permissions.is_supplier_owner(roles) or not supplier_id:
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    field = callback.data.split(":", 1)[1]

    prompts = {
        "name": "أدخل <b>اسم المورد</b> الجديد:",
        "activity": "أدخل <b>النشاط التجاري</b> الجديد:",
        "cities": "أدخل <b>المدن المخدومة</b> (افصل بفاصلة):\n(مثال: Москва, Екатеринбург)",
        "email": "أدخل <b>البريد الإلكتروني</b> الجديد:\n(يجب أن يكون بصيغة صحيحة)",
    }

    if field not in prompts:
        await callback.answer("❌ حقل غير صالح.", show_alert=True)
        return

    await state.update_data(update_field=field, supplier_id=supplier_id)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="upd_cancel")]
    ])

    await callback.message.edit_text(
        f"✏️ {prompts[field]}\n\n<i>للإلغاء اضغط الزر أدناه</i>",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await state.set_state(SupplierUpdateFlow.waiting_for_new_value)
    await callback.answer()


@router.callback_query(SupplierUpdateFlow.waiting_for_new_value, F.data == "upd_cancel")
async def cb_upd_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ تم إلغاء التعديل.", parse_mode="HTML")
    await callback.answer("تم الإلغاء")


@router.message(SupplierUpdateFlow.waiting_for_new_value)
async def process_upd_value(message: Message, state: FSMContext, user):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم إلغاء التعديل.")
        return

    data = await state.get_data()
    field = data.get("update_field")
    supplier_id = data.get("supplier_id")
    new_value = message.text.strip() if message.text else ""
    await state.clear()

    if not field or not supplier_id:
        await message.answer("❌ انتهت صلاحية الجلسة. أعد المحاولة.")
        return

    # Validation per field
    if field == "name":
        if len(new_value) < 3:
            await message.answer("⚠️ الاسم قصير جداً (3 أحرف على الأقل).")
            return
    elif field == "cities":
        new_value = [c.strip() for c in new_value.split(",") if c.strip()]
        if not new_value:
            await message.answer("⚠️ أدخل مدينة واحدة على الأقل.")
            return
    elif field == "email":
        if not EMAIL_REGEX.match(new_value):
            await message.answer(
                "⚠️ <b>صيغة البريد الإلكتروني غير صالحة.</b>\n\n"
                "يجب أن يكون بالشكل: name@domain.com"
            )
            return

    try:
        from app.database.models.supplier import Supplier

        async with get_session(silent=True) as session:
            supplier = await session.get(Supplier, supplier_id)
            if not supplier:
                await message.answer("❌ لم يتم العثور على بيانات المورد.")
                return

            old_value = getattr(supplier, field)

            if field == "name":
                supplier.name = new_value
            elif field == "activity":
                supplier.activity = new_value or None
            elif field == "cities":
                supplier.cities = new_value
            elif field == "email":
                supplier.contact_email = new_value or None

            await session.commit()

        # Audit log (fixed: log_event + AuditAction.UPDATE)
        try:
            from app.services.audit_service import audit_service

            await audit_service.log_event(
                company_id=None,
                entity_table="suppliers",
                entity_id=supplier_id,
                action=AuditAction.UPDATE,
                actor_user_id=user.id if user else None,
                old_data={field: str(old_value)},
                new_data={field: str(new_value)},
                reason="تحديث ذاتي عبر لوحة التحكم",
            )
        except Exception:
            logger.exception("Audit log failed for supplier update")

        display_value = new_value
        if field == "cities" and isinstance(new_value, list):
            display_value = ", ".join(new_value)

        await message.answer(
            f"✅ <b>تم التحديث بنجاح!</b>\n\n"
            f"📝 الحقل: {field}\n"
            f"➡️ القيمة الجديدة: {_esc(str(display_value))}",
            parse_mode="HTML"
        )

    except Exception:
        logger.exception("Error updating supplier field %s", field)
        await message.answer("❌ حدث خطأ أثناء التحديث. حاول لاحقاً.")


# ============================================================================
# Navigation: Back to Global Main Menu
# ============================================================================
@router.callback_query(F.data == "go_to_main_menu")
async def cb_go_to_main_menu(callback: CallbackQuery, roles: List[str]):
    from app.bot.keyboards.main_menu import get_keyboard_by_roles

    await callback.message.edit_text(
        "🏠 <b>القائمة الرئيسية</b>",
        reply_markup=get_keyboard_by_roles(roles),
        parse_mode="HTML"
    )
    await callback.answer()
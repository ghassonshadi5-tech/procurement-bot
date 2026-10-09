"""
Supplier Registration Handlers
Aligned with Schema v2.0, Permissions Matrix v2.1, Architecture v3.0

This module handles ONLY the supplier registration flow (FSM):
- /register → choose "Register as supplier"
- Supplier registration wizard (name, INN, activity, cities, email)
- Auto-creation of supplier record + user link + invite-based relation
- Welcome message + auto-show supplier dashboard

Dashboard, catalog, reports, excel export, and update-info are all
handled by supplier_panel.py. This file does NOT contain those anymore.

IMPROVEMENTS (this revision):
- Uses get_logger(__name__) → correlation IDs.
- Unscoped sessions use silent=True (bootstrap/global lookups).
- Sensitive log lines marked with extra={"security": True}.
- Logging uses %s formatting.
- Removed all duplicated handlers (dashboard entry, link requests,
  catalog flow, reports, excel export, update-info) — they now live
  exclusively in supplier_panel.py.
"""
import re
from typing import List, Optional, Any
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session

logger = get_logger(__name__)
router = Router(name="supplier_registration")

# Email regex pattern for strict validation
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

# Predefined activity options
ACTIVITY_OPTIONS = [
    ("🏗️ بناء ومقاولات", "building_construction"),
    ("🍽️ أغذية ومشروبات", "food_beverage"),
    ("⚙️ صيانة وخدمات فنية", "maintenance_services"),
    ("🏭 مواد بناء وصناعية", "industrial_materials"),
    ("💻 تكنولوجيا واتصالات", "technology_it"),
    ("🚛 نقل ولوجستيات", "transport_logistics"),
]


# ============================================================================
# Helpers
# ============================================================================
def _esc(value: Any) -> str:
    """Escape user input for HTML parse mode."""
    if value is None:
        return ""
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ============================================================================
# FSM: Supplier Registration
# ============================================================================
class SupplierRegFlow(StatesGroup):
    waiting_for_name = State()
    waiting_for_inn = State()
    waiting_for_activity_choice = State()
    waiting_for_activity_custom = State()
    waiting_for_cities = State()
    waiting_for_email = State()


# ============================================================================
# Entry Points
# ============================================================================
@router.callback_query(F.data == "register:supplier")
async def cb_start_supplier_reg(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupplierRegFlow.waiting_for_name)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="reg_cancel")]
    ])
    await callback.message.edit_text(
        "🏭 <b>تسجيل مورد جديد</b> (الخطوة 1/5)\n\n"
        "ما هو <b>اسم المورد</b> أو الشركة؟",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("register:supplier_from_invite:"))
async def cb_start_supplier_reg_from_invite(callback: CallbackQuery, state: FSMContext, user):
    """Start supplier registration flow with pre-linked company context."""
    invite_company_id = int(callback.data.split(":", 2)[2])

    await state.update_data(invite_company_id=invite_company_id, from_invite=True)
    await state.set_state(SupplierRegFlow.waiting_for_name)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="reg_cancel")]
    ])

    # Fetch company name dynamically
    company_name = "الشركة الداعية"
    try:
        from app.database.models.company import Company
        async with get_session(silent=True) as session:
            company = await session.get(Company, invite_company_id)
            if company:
                company_name = company.name
    except Exception:
        logger.exception("Failed to fetch company name for invite context")

    await callback.message.edit_text(
        "🏭 <b>تسجيل مورد جديد</b> (الخطوة 1/5)\n\n"
        f"🏢 سيتم ربطك تلقائياً بـ <b>{_esc(company_name)}</b> بعد اكتمال التسجيل.\n\n"
        "ما هو <b>اسم المورد</b> أو الشركة؟",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "reg_cancel")
async def cb_reg_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ تم إلغاء التسجيل.", parse_mode="HTML")
    await callback.answer("تم الإلغاء")


# ============================================================================
# Step 1: Name
# ============================================================================
@router.message(SupplierRegFlow.waiting_for_name)
async def process_reg_name(message: Message, state: FSMContext):
    name = message.text.strip() if message.text else ""
    if len(name) < 3:
        await message.answer("⚠️ الاسم قصير جداً (3 أحرف على الأقل).")
        return
    await state.update_data(supplier_name=name)
    skip_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ تخطي (اختياري)", callback_data="reg_skip_inn")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="reg_cancel")]
    ])
    await message.answer(
        f"✅ الاسم: {_esc(name)}\n\n<b>الخطوة 2/5:</b> ما هو الرقم الضريبي (ИНН)؟ (اختياري)",
        reply_markup=skip_kb, parse_mode="HTML"
    )
    await state.set_state(SupplierRegFlow.waiting_for_inn)


# ============================================================================
# Step 2: INN (Optional)
# ============================================================================
@router.callback_query(SupplierRegFlow.waiting_for_inn, F.data == "reg_skip_inn")
async def skip_inn(callback: CallbackQuery, state: FSMContext):
    await state.update_data(inn=None)
    await _show_activity_buttons(callback.message)
    await state.set_state(SupplierRegFlow.waiting_for_activity_choice)
    await callback.answer()


@router.message(SupplierRegFlow.waiting_for_inn)
async def process_reg_inn(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم إلغاء التسجيل.")
        return
    inn = message.text.strip() if message.text else ""
    if inn and not inn.isdigit():
        await message.answer("⚠️ INN يجب أن يكون أرقاماً فقط. أعد المحاولة أو اضغط /start للإلغاء.")
        return
    await state.update_data(inn=inn or None)
    await _show_activity_buttons(message)
    await state.set_state(SupplierRegFlow.waiting_for_activity_choice)


# ============================================================================
# Step 3: Activity (Buttons + Custom Option)
# ============================================================================
async def _show_activity_buttons(message: Message):
    """Display activity selection buttons."""
    buttons = []
    for label, code in ACTIVITY_OPTIONS:
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"reg_activity:{code}")])
    buttons.append([InlineKeyboardButton(text="✏️ نشاط آخر (كتابة حرة)", callback_data="reg_activity_custom")])
    buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="reg_cancel")])

    await message.answer(
        "<b>الخطوة 3/5:</b> اختر <b>النشاط التجاري</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@router.callback_query(SupplierRegFlow.waiting_for_activity_choice, F.data.startswith("reg_activity:"))
async def process_activity_choice(callback: CallbackQuery, state: FSMContext):
    activity_code = callback.data.split(":", 1)[1]
    # Map code back to Arabic label
    label_map = {code: label for label, code in ACTIVITY_OPTIONS}
    activity_text = label_map.get(activity_code, activity_code)

    await state.update_data(activity=activity_text)
    await callback.message.edit_text(
        f"✅ النشاط: {_esc(activity_text)}\n\n"
        "<b>الخطوة 4/5:</b> أدخل المدن التي تخدمها (افصل بفاصلة):\n(مثال: Москва, Екатеринбург)",
        parse_mode="HTML"
    )
    await state.set_state(SupplierRegFlow.waiting_for_cities)
    await callback.answer()


@router.callback_query(SupplierRegFlow.waiting_for_activity_choice, F.data == "reg_activity_custom")
async def prompt_custom_activity(callback: CallbackQuery, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="reg_cancel")]
    ])
    await callback.message.edit_text(
        "✏️ اكتب <b>النشاط التجاري</b> الخاص بك:\n(مثال: تجارة قطع غيار سيارات)",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await state.set_state(SupplierRegFlow.waiting_for_activity_custom)
    await callback.answer()


@router.message(SupplierRegFlow.waiting_for_activity_custom)
async def process_custom_activity(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم إلغاء التسجيل.")
        return
    activity = message.text.strip() if message.text else ""
    if len(activity) < 3:
        await message.answer("⚠️ النص قصير جداً (3 أحرف على الأقل).")
        return
    await state.update_data(activity=activity)
    await message.answer(
        f"✅ النشاط: {_esc(activity)}\n\n"
        "<b>الخطوة 4/5:</b> أدخل المدن التي تخدمها (افصل بفاصلة):\n(مثال: Москва, Екатеринбург)",
        parse_mode="HTML"
    )
    await state.set_state(SupplierRegFlow.waiting_for_cities)


# ============================================================================
# Step 4: Cities
# ============================================================================
@router.message(SupplierRegFlow.waiting_for_cities)
async def process_reg_cities(message: Message, state: FSMContext):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم إلغاء التسجيل.")
        return
    cities_text = message.text.strip() if message.text else ""
    cities = [c.strip() for c in cities_text.split(",") if c.strip()] if cities_text else []
    await state.update_data(cities=cities)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="reg_cancel")]
    ])
    await message.answer(
        "<b>الخطوة 5/5:</b> أدخل <b>البريد الإلكتروني</b> للتواصل:\n"
        "(إجباري — يجب أن يكون بصيغة صحيحة مثل: info@example.com)",
        reply_markup=cancel_kb, parse_mode="HTML"
    )
    await state.set_state(SupplierRegFlow.waiting_for_email)


# ============================================================================
# Step 5: Email (STRICT - Required, Validated, No Skip)
# ============================================================================
@router.message(SupplierRegFlow.waiting_for_email)
async def process_reg_email(message: Message, state: FSMContext, user):
    if message.text and message.text.strip() == "❌ إلغاء":
        await state.clear()
        await message.answer("❌ تم إلغاء التسجيل.")
        return

    email = message.text.strip() if message.text else ""

    # Strict email validation - cannot proceed without valid email
    if not email:
        await message.answer("⚠️ البريد الإلكتروني <b>إجباري</b>. الرجاء إدخال بريد صحيح أو اضغط ❌ إلغاء.")
        return

    if not EMAIL_REGEX.match(email):
        await message.answer(
            "⚠️ <b>صيغة البريد الإلكتروني غير صالحة.</b>\n\n"
            "يجب أن يكون بالشكل: name@domain.com\n"
            "مثال: info@company.ru\n\n"
            "الرجاء إعادة الإدخال أو اضغط ❌ إلغاء."
        )
        return

    data = await state.get_data()
    invite_company_id = data.get("invite_company_id")
    from_invite = data.get("from_invite", False)
    await state.clear()

    try:
        from app.database.models.supplier import Supplier
        from app.database.models.user import User as UserModel
        from app.services.user_service import user_service

        new_supplier_id = None

        async with get_session(silent=True) as session:
            # Create supplier directly (supplier_service.create_supplier doesn't exist)
            new_supplier = Supplier(
                name=data["supplier_name"],
                inn=data.get("inn"),
                activity=data.get("activity"),
                cities=data.get("cities", []),
                contact_email=email,
                status="pending",
                is_verified=False,
            )
            session.add(new_supplier)
            await session.flush()
            new_supplier_id = new_supplier.id

            # Link user to supplier
            if user:
                db_user = await session.get(UserModel, user.id)
                if db_user:
                    db_user.supplier_id = new_supplier_id

            # Auto-create relation if from invite
            if from_invite and invite_company_id:
                from app.database.models.supplier_relation import SupplierRelation
                existing_stmt = select(SupplierRelation).where(
                    SupplierRelation.company_id == invite_company_id,
                    SupplierRelation.supplier_id == new_supplier_id
                )
                existing = await session.execute(existing_stmt)
                if not existing.scalar_one_or_none():
                    new_rel = SupplierRelation(
                        company_id=invite_company_id,
                        supplier_id=new_supplier_id,
                        status="pending"
                    )
                    session.add(new_rel)
                    logger.info(
                        "Auto-created relation | company_id=%s | supplier_id=%s",
                        invite_company_id,
                        new_supplier_id,
                    )

            await session.commit()

        # Assign supplier_owner role outside the main transaction
        if user:
            try:
                await user_service.assign_role(
                    user_id=user.id,
                    role_code="supplier_owner",
                    granted_by=None
                )
            except Exception:
                logger.exception(
                    "Failed to assign supplier_owner role | user_id=%s",
                    user.id,
                )

        from app.bot.keyboards.main_menu import get_supplier_owner_keyboard
        welcome_msg = (
            "🎉 <b>تم تسجيل المورد بنجاح!</b>\n\n"
            "سيتم مراجعة طلبك من قبل إدارة المنصة.\n"
            "ستصلك إشعارات عند تفعيل الحساب."
        )
        if from_invite:
            welcome_msg += "\n\n🔗 <b>تم إرسال طلب ربط تلقائي للشركة الداعية.</b>"

        await message.answer(
            welcome_msg,
            reply_markup=get_supplier_owner_keyboard(),
            parse_mode="HTML"
        )

    except Exception:
        logger.exception("Supplier registration error")
        await message.answer("❌ حدث خطأ أثناء التسجيل. حاول لاحقاً.", parse_mode="HTML")
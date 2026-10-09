"""
Registration handlers - handles company creation and joining via tokens.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Flow:
1. New user clicks "Create new company" -> Enters name, phone, email, industry -> Pending approval.
2. New user clicks "Join existing company" -> Enters Invitation Token -> Auto-joins and gets role.

KEY CHANGES:
- No more `CompanyRequest` table. Companies are created directly with status='pending'.
- No more `invite_code` strings. Uses secure `CompanyInvitation` tokens.
- Fully async using Services layer.
- ✅ Added phone, email, and industry fields to company registration.
- ✅ Added similar name check with notification (not suggestions).
- ✅ Industry selection: 4 fixed options + "Other" (free text).
- ✅ Uniqueness validation for phone and email handled by Service Layer.
- ✅ Permanent Cancel Button in all steps.
- ✅ Fixed User Lookup error in finalize step.

IMPROVEMENTS (this revision):
- Uses get_logger(__name__) for correlation IDs.
- Unscoped sessions use silent=True (bootstrap checks before a company
  exists; role lookup is global data).
- FIXED: invitation.created_by → granted_by=None. The CompanyInvitation
  model has no created_by attribute, so the previous code would raise
  AttributeError on the join-company flow.
- Sensitive log lines marked with extra={"security": True}.
- Logging uses %s formatting.
"""
from typing import Optional, List
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func

from app.core.logger import get_logger
from app.config.settings import settings
from app.database.database import get_session
from app.database.models.user import User
from app.services.user_service import user_service
from app.services.company_service import company_service
from app.services.invite_service import invite_service
from app.bot.keyboards.main_menu import get_keyboard_by_roles
from app.core import permissions

logger = get_logger(__name__)
router = Router(name="registration")


# ============================================================================
# Industry Options (4 fixed + Other)
# ============================================================================
INDUSTRY_OPTIONS = [
    ("manufacturing", "🏭 تصنيع وإنتاج"),
    ("construction", "🏗️ بناء ومقاولات"),
    ("food_beverage", "🍽️ أغذية ومشروبات"),
    ("services", "🔧 خدمات وصيانة"),
    ("other", "✏️ أخرى (كتابة حرة)"),
]


# ============================================================================
# FSM States
# ============================================================================
class RegistrationFlow(StatesGroup):
    """States for the registration process."""
    waiting_for_company_name = State()
    waiting_for_name_confirmation = State()
    waiting_for_phone = State()
    waiting_for_email = State()
    waiting_for_industry = State()
    waiting_for_industry_other = State()
    waiting_for_invite_token = State()


# ============================================================================
# Helper: HTML Escape
# ============================================================================
def _esc(value):
    """Escape user input for HTML parse mode."""
    if value is None:
        return ""
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================================
# Helper: Get Cancel Keyboard
# ============================================================================
def get_cancel_keyboard():
    """Returns a simple inline keyboard with just a Cancel button."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء العملية", callback_data="reg_cancel_flow")]
    ])


# ============================================================================
# Command: /register
# ============================================================================
@router.message(Command("register"))
async def cmd_register(message: Message, user: Optional[User], roles: List[str]):
    """Handle /register command - show registration options."""

    # If user is already registered, redirect to dashboard
    if user and roles:
        await message.answer(
            "✅ أنت مسجل بالفعل في النظام.\n"
            "استخدم /start للوصول إلى لوحة التحكم."
        )
        return

    registration_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 إنشاء شركة جديدة", callback_data="register:create_company")],
        [InlineKeyboardButton(text="🔑 الانضمام لشركة موجودة", callback_data="register:join_company")]
    ])

    await message.answer(
        f"👋 مرحباً بك <b>{message.from_user.full_name}</b>!\n\n"
        f"أنت جديد في نظام إدارة المشتريات.\n"
        f"اختر أحد الخيارات التالية:\n\n"
        f"🏢 <b>إنشاء شركة جديدة</b> - تتطلب موافقة مدير المنصة.\n"
        f"🔑 <b>الانضمام لشركة موجودة</b> - تتطلب رمز دعوة (Token) من مدير الشركة.",
        reply_markup=registration_keyboard,
        parse_mode="HTML"
    )


# ============================================================================
# Callback: Registration Option Selection
# ============================================================================
@router.callback_query(F.data.startswith("register:"))
async def callback_register(callback: CallbackQuery, state: FSMContext, user: Optional[User]):
    """Handle registration option selection."""
    action = callback.data.split(":", 1)[1]

    # Ensure user exists in DB before proceeding
    if not user:
        user = await user_service.get_or_create_user(
            telegram_id=callback.from_user.id,
            full_name=callback.from_user.full_name or "Unknown",
            language=callback.from_user.language_code or "ar"
        )

    if action == "create_company":
        await callback.message.edit_text(
            "🏢 <b>إنشاء شركة جديدة</b>\n\n"
            "سأطلب منك بعض المعلومات لتسجيل شركتك على المنصة.\n"
            "يمكنك إلغاء العملية في أي وقت بالضغط على زر 'إلغاء' أدناه.\n\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "📝 <b>الخطوة 1 من 4:</b>\n"
            "ما هو <b>اسم الشركة</b> كاملاً؟\n\n"
            "(مثال: شركة النور للتجارة والمقاولات)",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(RegistrationFlow.waiting_for_company_name)

    elif action == "join_company":
        await callback.message.edit_text(
            "🔑 <b>الانضمام لشركة موجودة</b>\n\n"
            "الرجاء إدخال <b>رمز الدعوة (Token)</b> الذي حصلت عليه من مدير الشركة:\n"
            "(رمز طويل مكون من أرقام وحروف)\n\n"
            "يمكنك إلغاء العملية بالضغط على زر 'إلغاء' أدناه.",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(RegistrationFlow.waiting_for_invite_token)

    await callback.answer()


# ============================================================================
# Global Cancel Handler
# ============================================================================
@router.callback_query(F.data == "reg_cancel_flow")
async def handle_global_cancel(callback: CallbackQuery, state: FSMContext):
    """Cancel registration at any step via button."""
    current_state = await state.get_state()
    if current_state and current_state.startswith("RegistrationFlow:"):
        await state.clear()
        await callback.message.edit_text(
            "❌ تم إلغاء عملية التسجيل.\n\n"
            "استخدم /start للعودة إلى القائمة الرئيسية."
        )
        await callback.answer("تم الإلغاء.")


# Also keep the text-based cancel as backup
@router.message(F.text == "❌ إلغاء")
async def cancel_registration_any(message: Message, state: FSMContext):
    """Cancel registration at any step via text."""
    current_state = await state.get_state()
    if current_state and current_state.startswith("RegistrationFlow:"):
        await state.clear()
        await message.answer(
            "❌ تم إلغاء عملية التسجيل.\n\n"
            "استخدم /start للعودة إلى القائمة الرئيسية."
        )


# ============================================================================
# FSM Step 1: Process Company Name + Uniqueness + Similar Name Check
# ============================================================================
@router.message(RegistrationFlow.waiting_for_company_name)
async def process_company_name(message: Message, state: FSMContext, user: User):
    """Process company name input with strict uniqueness and similar name warning."""
    company_name = message.text.strip()

    if not company_name or len(company_name) < 3:
        await message.answer("❌ اسم الشركة قصير جداً. (3 أحرف على الأقل)")
        return

    # Store the name temporarily
    await state.update_data(company_name=company_name)

    # STEP 1: Check EXACT match (strict uniqueness)
    try:
        async with get_session(silent=True) as session:
            from app.database.models.company import Company, CompanyStatus
            stmt = select(Company.id).where(
                func.lower(func.trim(Company.name)) == company_name.strip().lower(),
                Company.status.in_([CompanyStatus.pending, CompanyStatus.active])
            )
            result = await session.execute(stmt)
            exact_match = result.scalar_one_or_none()
    except Exception:
        logger.exception("Error checking exact name match")
        exact_match = None

    if exact_match:
        # STRICT: Exact match found - block registration immediately
        await message.answer(
            f"❌ <b>تعذر إتمام التسجيل</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"اسم الشركة <b>«{_esc(company_name)}»</b> مستخدم بالفعل من قبل شركة أخرى نشطة أو معلّقة.\n\n"
            f"💡 الرجاء اختيار اسم مختلف لشركتك.",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        # Stay in the same state so user can try again
        return

    # STEP 2: Check SIMILAR names (warning only)
    try:
        similar_names = await company_service.find_similar_names(company_name)
    except Exception:
        logger.exception("Error checking similar names")
        similar_names = []

    if similar_names:
        # WARNING: Similar names found - let user decide
        names_list = "\n".join([f"   • {_esc(name)}" for name in similar_names])

        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ نعم، متابعة", callback_data="reg_confirm_name:yes"),
                InlineKeyboardButton(text="❌ تغيير الاسم", callback_data="reg_confirm_name:no")
            ],
            [InlineKeyboardButton(text="🚫 إلغاء كامل", callback_data="reg_cancel_flow")]
        ])

        await message.answer(
            f"⚠️ <b>تنبيه: توجد شركات مسجلة بأسماء قريبة</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"{names_list}\n\n"
            f"هل أنت متأكد من المتابعة بهذا الاسم؟\n"
            f"<b>{_esc(company_name)}</b>",
            reply_markup=confirm_keyboard,
            parse_mode="HTML"
        )
        await state.set_state(RegistrationFlow.waiting_for_name_confirmation)
    else:
        # No match, no similar - proceed directly to phone
        await _ask_for_phone(message)
        await state.set_state(RegistrationFlow.waiting_for_phone)


# ============================================================================
# FSM: Name Confirmation Callbacks
# ============================================================================
@router.callback_query(RegistrationFlow.waiting_for_name_confirmation, F.data == "reg_confirm_name:yes")
async def confirm_name_yes(callback: CallbackQuery, state: FSMContext):
    """User confirms the company name despite similar names."""
    await _ask_for_phone(callback.message)
    await state.set_state(RegistrationFlow.waiting_for_phone)
    await callback.answer()


@router.callback_query(RegistrationFlow.waiting_for_name_confirmation, F.data == "reg_confirm_name:no")
async def confirm_name_no(callback: CallbackQuery, state: FSMContext):
    """User wants to change the company name."""
    await callback.message.edit_text(
        "🏢 <b>تغيير اسم الشركة</b>\n\n"
        "📝 الرجاء إدخال <b>اسم الشركة</b> الجديد:\n\n"
        "(مثال: شركة النور للتجارة والمقاولات)",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(RegistrationFlow.waiting_for_company_name)
    await callback.answer()


# ============================================================================
# Helper: Ask for Phone Number
# ============================================================================
async def _ask_for_phone(message: Message):
    """Ask user for company phone number."""
    await message.answer(
        "✅ <b>الخطوة 2 من 4:</b>\n"
        "ما هو <b>رقم هاتف الشركة</b>؟\n\n"
        "مثال: +79001234567 أو 89001234567\n\n"
        "<i>💡 يجب أن يكون الرقم فريداً وغير مستخدم من قبل شركة أخرى.</i>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )


# ============================================================================
# FSM Step 2: Process Phone Number (with immediate uniqueness check)
# ============================================================================
@router.message(RegistrationFlow.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    """Process company phone number input with immediate uniqueness check."""
    phone_raw = message.text.strip()

    # Normalize phone: keep digits only
    phone_normalized = ''.join(c for c in phone_raw if c.isdigit())

    if len(phone_normalized) < 7:
        await message.answer("❌ رقم الهاتف غير صحيح. يجب أن يحتوي على 7 أرقام على الأقل.")
        return

    # IMMEDIATE UNIQUENESS CHECK
    try:
        async with get_session(silent=True) as session:
            from app.database.models.company import Company, CompanyStatus
            stmt = select(Company.name).where(
                Company.phone_number == phone_normalized,
                Company.status.in_([CompanyStatus.pending, CompanyStatus.active])
            )
            result = await session.execute(stmt)
            existing_company = result.scalar_one_or_none()
    except Exception:
        logger.exception("Error checking phone uniqueness")
        existing_company = None

    if existing_company:
        # Phone already used - block immediately
        await message.answer(
            f"❌ <b>تعذر إتمام التسجيل</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"رقم الهاتف <code>{_esc(phone_raw)}</code> مستخدم بالفعل من قبل شركة أخرى:\n"
            f"<b>{_esc(existing_company)}</b>\n\n"
            f"💡 الرجاء استخدام رقم هاتف مختلف.",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        # Stay in the same state so user can try again
        return

    # Store both raw (for display) and normalized (for DB/uniqueness check)
    await state.update_data(phone_raw=phone_raw, phone_number=phone_normalized)

    await message.answer(
        "✅ <b>الخطوة 3 من 4:</b>\n"
        "ما هو <b>البريد الإلكتروني</b> للشركة؟\n\n"
        "مثال: info@company.ru\n\n"
        "<i>💡 يجب أن يكون البريد فريداً وغير مستخدم من قبل شركة أخرى.</i>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(RegistrationFlow.waiting_for_email)


# ============================================================================
# FSM Step 3: Process Email (with immediate uniqueness check)
# ============================================================================
@router.message(RegistrationFlow.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    """Process company email input with immediate uniqueness check."""
    email = message.text.strip().lower()

    if "@" not in email or "." not in email:
        await message.answer("❌ البريد الإلكتروني غير صحيح. الرجاء المحاولة مرة أخرى.")
        return

    # IMMEDIATE UNIQUENESS CHECK
    try:
        async with get_session(silent=True) as session:
            from app.database.models.company import Company, CompanyStatus
            stmt = select(Company.name).where(
                func.lower(func.trim(Company.email)) == email,
                Company.status.in_([CompanyStatus.pending, CompanyStatus.active])
            )
            result = await session.execute(stmt)
            existing_company = result.scalar_one_or_none()
    except Exception:
        logger.exception("Error checking email uniqueness")
        existing_company = None

    if existing_company:
        # Email already used - block immediately
        await message.answer(
            f"❌ <b>تعذر إتمام التسجيل</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"البريد الإلكتروني <code>{_esc(email)}</code> مستخدم بالفعل من قبل شركة أخرى:\n"
            f"<b>{_esc(existing_company)}</b>\n\n"
            f"💡 الرجاء استخدام بريد إلكتروني مختلف.",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        # Stay in the same state so user can try again
        return

    await state.update_data(email=email)

    # Show industry selection keyboard
    industry_buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"reg_industry:{code}")]
        for code, label in INDUSTRY_OPTIONS
    ]
    industry_buttons.append([InlineKeyboardButton(text="🚫 إلغاء كامل", callback_data="reg_cancel_flow")])

    industry_keyboard = InlineKeyboardMarkup(inline_keyboard=industry_buttons)

    await message.answer(
        "✅ <b>الخطوة 4 من 4:</b>\n"
        "في أي <b>مجال تعمل الشركة</b>؟\n\n"
        "اختر من القائمة أدناه:",
        reply_markup=industry_keyboard,
        parse_mode="HTML"
    )
    await state.set_state(RegistrationFlow.waiting_for_industry)


# ============================================================================
# FSM Step 4a: Process Industry Selection
# ============================================================================
@router.callback_query(RegistrationFlow.waiting_for_industry, F.data.startswith("reg_industry:"))
async def process_industry_selection(callback: CallbackQuery, state: FSMContext):
    """Process industry selection from predefined options."""
    selected_code = callback.data.split(":", 1)[1]

    if selected_code == "other":
        # Ask for free text
        await callback.message.edit_text(
            "✏️ <b>حدد مجال عمل الشركة:</b>\n\n"
            "اكتب مجال العمل بالتفصيل:\n\n"
            "(مثال: تجارة قطع غيار السيارات، خدمات IT...)",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(RegistrationFlow.waiting_for_industry_other)
    else:
        # Map code to display label
        label_map = {code: label for code, label in INDUSTRY_OPTIONS}
        industry_label = label_map.get(selected_code, selected_code)

        await state.update_data(industry=selected_code, industry_label=industry_label)
        await _finalize_company_registration(
            callback.message, state, callback.from_user.id
        )

    await callback.answer()


# ============================================================================
# FSM Step 4b: Process Industry "Other" Free Text
# ============================================================================
@router.message(RegistrationFlow.waiting_for_industry_other)
async def process_industry_other(message: Message, state: FSMContext):
    """Process free text industry input."""
    industry_text = message.text.strip()

    if len(industry_text) < 3:
        await message.answer("❌ الوصف قصير جداً. الرجاء كتابة وصف أطول (3 أحرف على الأقل).")
        return

    await state.update_data(industry="other", industry_label=industry_text)
    await _finalize_company_registration(message, state, message.from_user.id)


# ============================================================================
# Finalize Company Registration
# ============================================================================
async def _finalize_company_registration(message: Message, state: FSMContext, tg_user_id: int):
    """Create the company with all collected data and notify Super Admin."""
    data = await state.get_data()
    await state.clear()

    company_name = data.get("company_name", "")
    phone_raw = data.get("phone_raw", "")
    phone_normalized = data.get("phone_number", "")
    email = data.get("email", "")
    industry = data.get("industry", "")
    industry_label = data.get("industry_label", "")

    loading_msg = await message.answer("⏳ جارٍ حفظ طلب التسجيل...")

    try:
        # Get user by Telegram ID explicitly passed to ensure correctness
        async with get_session(silent=True) as session:
            stmt = select(User).where(User.telegram_user_id == tg_user_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

        if not user:
            logger.error(
                "Registration: user not found for TG_ID %s",
                tg_user_id,
                extra={"security": True},
            )
            await loading_msg.edit_text(
                "❌ حدث خطأ داخلي: لم يتم العثور على حسابك في النظام. يرجى التواصل مع الدعم."
            )
            return

        # Create the company using the updated service
        # The service will handle uniqueness checks internally
        try:
            new_company = await company_service.create_company(
                name=company_name,
                created_by_user_id=user.id,
                phone_number=phone_normalized,
                email=email,
                industry_code=industry,
                industry_other_text=industry_label if industry == "other" else None,
            )
        except ValueError as ve:
            # Uniqueness violation caught by service
            await loading_msg.edit_text(
                f"❌ <b>تعذر إتمام التسجيل</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"{str(ve)}\n\n"
                f"💡 الرجاء استخدام بيانات تواصل مختلفة.",
                parse_mode="HTML"
            )
            return

        await loading_msg.edit_text(
            f"📨 <b>تم استلام طلبك بنجاح!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏢 الشركة: <b>{_esc(company_name)}</b>\n"
            f"📞 الهاتف: <b>{_esc(phone_raw)}</b>\n"
            f"📧 الإيميل: <b>{_esc(email)}</b>\n"
            f"🏷️ المجال: <b>{_esc(industry_label)}</b>\n"
            f"👤 مقدم الطلب: <b>{_esc(message.from_user.full_name)}</b>\n\n"
            f"⏳ تم إرسال طلبك إلى مدير المنصة للمراجعة والاعتماد.\n"
            f"📢 ستتلقى إشعاراً فور الموافقة على تسجيل شركتك.",
            parse_mode="HTML"
        )

        # Notify Super Admin
        super_admin_id = settings.SUPER_ADMIN_TELEGRAM_ID
        if super_admin_id:
            admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ اعتماد الشركة",
                        callback_data=f"sa_approve_company:{new_company.id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ رفض",
                        callback_data=f"sa_reject_company_start:{new_company.id}",
                    ),
                ]
            ])

            try:
                await message.bot.send_message(
                    chat_id=int(super_admin_id),
                    text=(
                        f"🔔 <b>طلب تسجيل شركة جديد!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━\n\n"
                        f"🏢 الاسم: <b>{_esc(company_name)}</b>\n"
                        f"📞 الهاتف: <b>{_esc(phone_raw)}</b>\n"
                        f"📧 الإيميل: <b>{_esc(email)}</b>\n"
                        f"🏷️ المجال: <b>{_esc(industry_label)}</b>\n\n"
                        f"👤 مقدم الطلب: {_esc(message.from_user.full_name)}\n"
                        f"🆔 TG ID: <code>{message.from_user.id}</code>\n"
                        f"📱 Username: @{message.from_user.username or 'N/A'}\n"
                        f"🔢 رقم الشركة: <code>{new_company.id}</code>\n\n"
                        f"هل تريد اعتماد هذه الشركة؟"
                    ),
                    reply_markup=admin_keyboard,
                    parse_mode="HTML"
                )
            except Exception:
                logger.exception("Failed to notify Super Admin about new company")

    except Exception:
        logger.exception("Failed to create company")
        await loading_msg.edit_text(
            "❌ حدث خطأ أثناء حفظ الطلب.\n"
            "الرجاء المحاولة مرة أخرى لاحقًا."
        )


# ============================================================================
# FSM Step: Process Invite Token (Join Company)
# ============================================================================
@router.message(RegistrationFlow.waiting_for_invite_token)
async def process_invite_token(message: Message, state: FSMContext, user: User):
    """Process invite token and join the user to the company."""
    token = message.text.strip()

    # 1. Validate the token
    is_valid, invitation, error_msg = await invite_service.validate_invitation(token)

    if not is_valid:
        await message.answer(
            f"❌ {error_msg}\n\n"
            f"الرجاء التحقق من الرمز والمحاولة مرة أخرى، أو استخدام /start للعودة."
        )
        # Don't clear state, let them try again
        return

    # 2. Accept the invitation (Links user to company)
    success, invitation, accept_error = await invite_service.accept_invitation(
        token=token,
        user_id=user.id
    )

    if not success:
        await message.answer(f"❌ {accept_error}")
        await state.clear()
        return

    # 3. Fetch Role and Assign it
    from app.database.models.role import Role

    async with get_session(silent=True) as session:
        stmt = select(Role).where(Role.id == invitation.role_id)
        result = await session.execute(stmt)
        role = result.scalar_one()

        # FIXED: CompanyInvitation has no created_by attribute.
        # granted_by is left as None for invite-based assignments.
        await user_service.assign_role(
            user_id=user.id,
            role_code=role.code,
            granted_by=None,
        )

        # Fetch company name
        company = await company_service.get_company_by_id(invitation.company_id)
        company_name = company.name if company else "الشركة"

    # 4. Send welcome message
    from app.bot.handlers.start import get_primary_role_display
    role_emoji, role_text = get_primary_role_display([role.code])
    keyboard = get_keyboard_by_roles([role.code])

    welcome_text = (
        f"🎉 <b>تم الانضمام بنجاح!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏢 الشركة: <b>{company_name}</b>\n"
        f"👤 دورك: {role_emoji} <b>{role_text}</b>\n\n"
        f"👋 مرحباً بك {message.from_user.full_name}!\n"
        f"استخدم /start للوصول إلى القائمة الرئيسية."
    )

    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="HTML")
    await state.clear()
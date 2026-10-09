"""
Start handlers - Main menu and dashboard.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Handles: /start, Home button, Deep Link invites (tokens), and routing based on user roles.

CHANGE LOG:
- FIXED (critical): process_supplier_invitation now uses get_session(company_id=...)
  for writes (was silent=True, bypassing RLS).
- Added defensive `if not user` check in process_supplier_invitation.
- Added AuditLog entries for invite acceptance (inv_, emp_, sup_).
- cb_set_language now handles rollback explicitly.
"""
from typing import List, Optional
from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.core.logger import get_logger
from app.core.i18n import t, get_supported_languages, is_supported
from app.database.models.user import User, UserStatus
from app.database.models.enums import CompanyStatus, AuditAction
from app.database.models.audit_log import AuditLog
from app.services.user_service import user_service
from app.services.invite_service import invite_service
from app.services.company_service import company_service
from app.services.supplier_service import supplier_service
from app.core import permissions
from app.bot.keyboards.main_menu import (
    get_super_admin_keyboard,
    get_keyboard_by_roles,
)
from app.database.database import get_session

logger = get_logger(__name__)
router = Router(name="start")


# ============================================================================
# Helper: Get role display info (translated)
# ============================================================================
def get_primary_role_display(roles: List[str], lang: str = "ar") -> tuple[str, str]:
    """
    Get emoji and translated text for the user's primary role.
    Priority: company_owner > manager > supplier_owner > supplier_manager > ...
    """
    role_key_map = {
        "super_admin": "role_super_admin",
        "company_owner": "role_company_owner",
        "manager": "role_manager",
        "accountant": "role_accountant",
        "storekeeper": "role_storekeeper",
        "requester": "role_requester",
        "supplier_owner": "role_supplier_owner",
        "supplier_manager": "role_supplier_manager",
        "supplier_employee": "role_supplier_employee",
    }
    role_emoji_map = {
        "super_admin": "👑",
        "company_owner": "👑",
        "manager": "💼",
        "accountant": "🧮",
        "storekeeper": "📦",
        "requester": "👷",
        "supplier_owner": "🏭",
        "supplier_manager": "💼",
        "supplier_employee": "👷",
    }
    defaults = {
        "super_admin": "مدير المنصة",
        "company_owner": "مالك الشركة",
        "manager": "مدير المشتريات",
        "accountant": "محاسب",
        "storekeeper": "أمين مستودع",
        "requester": "موظف",
        "supplier_owner": "مالك المورد",
        "supplier_manager": "مدير المورد",
        "supplier_employee": "موظف المورد",
    }

    # Priority order (first match wins)
    for role_code in role_key_map.keys():
        if role_code in roles:
            emoji = role_emoji_map.get(role_code, "👤")
            text = t(role_key_map[role_code], lang=lang)
            if text == role_key_map[role_code]:
                text = defaults.get(role_code, role_code)
            return (emoji, text)

    user_text = t("role_user", lang=lang)
    if user_text == "role_user":
        user_text = "مستخدم"
    return ("👤", user_text)


# ============================================================================
# Helper: Show dashboard based on roles
# ============================================================================
async def show_dashboard_for_user(
    message: Message,
    user: User,
    roles: List[str],
    lang: str = "ar",
):
    """Show the appropriate dashboard for a registered user based on their roles."""
    if permissions.is_super_admin(roles):
        kb = get_super_admin_keyboard()
        await message.answer(
            t("super_admin_dashboard", lang=lang)
            if t("super_admin_dashboard", lang=lang) != "super_admin_dashboard"
            else "👑 <b>لوحة التحكم العامة (Super Admin)</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    if not roles:
        await message.answer(
            t("no_roles_assigned", lang=lang)
            if t("no_roles_assigned", lang=lang) != "no_roles_assigned"
            else "⚠️ حسابك موجود في النظام ولكن لم يتم تعيين أي دور لك بعد.\nيرجى التواصل مع مدير النظام."
        )
        return

    role_emoji, role_text = get_primary_role_display(roles, lang=lang)

    keyboard = get_keyboard_by_roles(roles)

    # Determine context (Company or Supplier)
    if permissions.is_buyer_side(roles):
        context_text = t("dashboard_company", lang=lang)
        if context_text == "dashboard_company":
            context_text = "نظام إدارة المشتريات (الشركة)"
        context_emoji = "🏢"
    elif permissions.is_supplier_side(roles):
        context_text = t("dashboard_supplier", lang=lang)
        if context_text == "dashboard_supplier":
            context_text = "لوحة تحكم المورد"
        context_emoji = "🏭"
    else:
        context_text = t("dashboard_generic", lang=lang)
        if context_text == "dashboard_generic":
            context_text = "النظام"
        context_emoji = "👤"

    welcome_template = t("welcome_dashboard", lang=lang)
    if welcome_template == "welcome_dashboard":
        welcome_template = (
            "👋 مرحباً بك <b>{name}</b>!\n\n"
            "{context}\n"
            "👤 دورك: {role}\n\n"
            "📋 استخدم الأزرار أدناه للتنقل، أو اكتب /help للمساعدة."
        )

    welcome_text = welcome_template.format(
        name=message.from_user.full_name,
        context=f"{context_emoji} <b>{context_text}</b>",
        role=f"{role_emoji} <b>{role_text}</b>",
    )

    if keyboard:
        await message.answer(welcome_text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message.answer(
            welcome_text + "\n⚠️ لم يتم العثور على لوحة مفاتيح مناسبة لدورك.",
            parse_mode="HTML",
        )


# ============================================================================
# Helper: Show registration menu (new users)
# ============================================================================
async def show_registration_menu(message: Message, lang: str = "ar"):
    """
    Show the registration options for a brand-new user.

    Called:
    - After a new user selects their language.
    - From cmd_start when the user has no roles yet.
    """
    registration_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 إنشاء شركة جديدة", callback_data="register:create_company")],
        [InlineKeyboardButton(text="🔑 الانضمام لشركة موجودة", callback_data="register:join_company")],
        [InlineKeyboardButton(text="🏭 تسجيل كمورد", callback_data="register:supplier")],
    ])

    welcome_text = t("new_user_welcome", lang=lang)
    if welcome_text == "new_user_welcome":
        # Fallback to Arabic if the key is missing in the locale file
        welcome_text = (
            f"👋 مرحباً بك <b>{message.from_user.full_name}</b>!\n\n"
            f"أنت جديد في نظام إدارة المشتريات.\n"
            f"اختر أحد الخيارات التالية للبدء:"
        )

    await message.answer(
        welcome_text,
        reply_markup=registration_keyboard,
        parse_mode="HTML",
    )
    """
Start handlers - Main menu and dashboard.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Handles: /start, Home button, Deep Link invites (tokens), and routing based on user roles.

CHANGE LOG:
- FIXED (critical): process_supplier_invitation now uses get_session(company_id=...)
  for writes (was silent=True, bypassing RLS).
- Added defensive `if not user` check in process_supplier_invitation.
- Added AuditLog entries for invite acceptance (inv_, emp_, sup_).
- cb_set_language now handles rollback explicitly.
"""
from typing import List, Optional
from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.core.logger import get_logger
from app.core.i18n import t, get_supported_languages, is_supported
from app.database.models.user import User, UserStatus
from app.database.models.enums import CompanyStatus, AuditAction
from app.database.models.audit_log import AuditLog
from app.services.user_service import user_service
from app.services.invite_service import invite_service
from app.services.company_service import company_service
from app.services.supplier_service import supplier_service
from app.core import permissions
from app.bot.keyboards.main_menu import (
    get_super_admin_keyboard,
    get_keyboard_by_roles,
)
from app.database.database import get_session

logger = get_logger(__name__)
router = Router(name="start")


# ============================================================================
# Helper: Get role display info (translated)
# ============================================================================
def get_primary_role_display(roles: List[str], lang: str = "ar") -> tuple[str, str]:
    """
    Get emoji and translated text for the user's primary role.
    Priority: company_owner > manager > supplier_owner > supplier_manager > ...
    """
    role_key_map = {
        "super_admin": "role_super_admin",
        "company_owner": "role_company_owner",
        "manager": "role_manager",
        "accountant": "role_accountant",
        "storekeeper": "role_storekeeper",
        "requester": "role_requester",
        "supplier_owner": "role_supplier_owner",
        "supplier_manager": "role_supplier_manager",
        "supplier_employee": "role_supplier_employee",
    }
    role_emoji_map = {
        "super_admin": "👑",
        "company_owner": "👑",
        "manager": "💼",
        "accountant": "🧮",
        "storekeeper": "📦",
        "requester": "👷",
        "supplier_owner": "🏭",
        "supplier_manager": "💼",
        "supplier_employee": "👷",
    }
    defaults = {
        "super_admin": "مدير المنصة",
        "company_owner": "مالك الشركة",
        "manager": "مدير المشتريات",
        "accountant": "محاسب",
        "storekeeper": "أمين مستودع",
        "requester": "موظف",
        "supplier_owner": "مالك المورد",
        "supplier_manager": "مدير المورد",
        "supplier_employee": "موظف المورد",
    }

    # Priority order (first match wins)
    for role_code in role_key_map.keys():
        if role_code in roles:
            emoji = role_emoji_map.get(role_code, "👤")
            text = t(role_key_map[role_code], lang=lang)
            if text == role_key_map[role_code]:
                text = defaults.get(role_code, role_code)
            return (emoji, text)

    user_text = t("role_user", lang=lang)
    if user_text == "role_user":
        user_text = "مستخدم"
    return ("👤", user_text)


# ============================================================================
# Helper: Show dashboard based on roles
# ============================================================================
async def show_dashboard_for_user(
    message: Message,
    user: User,
    roles: List[str],
    lang: str = "ar",
):
    """Show the appropriate dashboard for a registered user based on their roles."""
    if permissions.is_super_admin(roles):
        kb = get_super_admin_keyboard()
        await message.answer(
            t("super_admin_dashboard", lang=lang)
            if t("super_admin_dashboard", lang=lang) != "super_admin_dashboard"
            else "👑 <b>لوحة التحكم العامة (Super Admin)</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    if not roles:
        await message.answer(
            t("no_roles_assigned", lang=lang)
            if t("no_roles_assigned", lang=lang) != "no_roles_assigned"
            else "⚠️ حسابك موجود في النظام ولكن لم يتم تعيين أي دور لك بعد.\nيرجى التواصل مع مدير النظام."
        )
        return

    role_emoji, role_text = get_primary_role_display(roles, lang=lang)

    keyboard = get_keyboard_by_roles(roles)

    # Determine context (Company or Supplier)
    if permissions.is_buyer_side(roles):
        context_text = t("dashboard_company", lang=lang)
        if context_text == "dashboard_company":
            context_text = "نظام إدارة المشتريات (الشركة)"
        context_emoji = "🏢"
    elif permissions.is_supplier_side(roles):
        context_text = t("dashboard_supplier", lang=lang)
        if context_text == "dashboard_supplier":
            context_text = "لوحة تحكم المورد"
        context_emoji = "🏭"
    else:
        context_text = t("dashboard_generic", lang=lang)
        if context_text == "dashboard_generic":
            context_text = "النظام"
        context_emoji = "👤"

    welcome_template = t("welcome_dashboard", lang=lang)
    if welcome_template == "welcome_dashboard":
        welcome_template = (
            "👋 مرحباً بك <b>{name}</b>!\n\n"
            "{context}\n"
            "👤 دورك: {role}\n\n"
            "📋 استخدم الأزرار أدناه للتنقل، أو اكتب /help للمساعدة."
        )

    welcome_text = welcome_template.format(
        name=message.from_user.full_name,
        context=f"{context_emoji} <b>{context_text}</b>",
        role=f"{role_emoji} <b>{role_text}</b>",
    )

    if keyboard:
        await message.answer(welcome_text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message.answer(
            welcome_text + "\n⚠️ لم يتم العثور على لوحة مفاتيح مناسبة لدورك.",
            parse_mode="HTML",
        )


# ============================================================================
# Helper: Show registration menu (new users)
# ============================================================================
async def show_registration_menu(message: Message, lang: str = "ar"):
    """
    Show the registration options for a brand-new user.

    Called:
    - After a new user selects their language.
    - From cmd_start when the user has no roles yet.
    """
    registration_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 إنشاء شركة جديدة", callback_data="register:create_company")],
        [InlineKeyboardButton(text="🔑 الانضمام لشركة موجودة", callback_data="register:join_company")],
        [InlineKeyboardButton(text="🏭 تسجيل كمورد", callback_data="register:supplier")],
    ])

    welcome_text = t("new_user_welcome", lang=lang)
    if welcome_text == "new_user_welcome":
        # Fallback to Arabic if the key is missing in the locale file
        welcome_text = (
            f"👋 مرحباً بك <b>{message.from_user.full_name}</b>!\n\n"
            f"أنت جديد في نظام إدارة المشتريات.\n"
            f"اختر أحد الخيارات التالية للبدء:"
        )

    await message.answer(
        welcome_text,
        reply_markup=registration_keyboard,
        parse_mode="HTML",
    )


# ============================================================================
# Language Selection Screen
# ============================================================================
def build_language_keyboard() -> InlineKeyboardMarkup:
    """Build the language selection inline keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇸🇦 العربية", callback_data="set_lang:ar")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang:en")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang:ru")],
    ])


@router.callback_query(F.data.startswith("set_lang:"))
async def cb_set_language(callback, user: Optional[User], state):
    """Handle language selection."""
    lang_code = callback.data.split(":", 1)[1]

    if not is_supported(lang_code):
        await callback.answer("❌ Unsupported language", show_alert=True)
        return

    # Save language to user record (if user exists)
    if user:
        try:
            async with get_session(silent=True) as session:
                try:
                    from app.database.models.user import User as UserModel
                    db_user = await session.get(UserModel, user.id)
                    if db_user:
                        db_user.language = lang_code
                        await session.commit()
                    logger.info(
                        "User language updated | user_id=%s | lang=%s",
                        user.id,
                        lang_code,
                    )
                except Exception:
                    await session.rollback()
                    raise
        except Exception:
            logger.exception("Failed to update user language")

    # Acknowledge language selection
    confirmation = t("language_set", lang=lang_code)
    if confirmation == "language_set":
        defaults = {
            "ar": "✅ تم تعيين اللغة إلى العربية.",
            "en": "✅ Language set to English.",
            "ru": "✅ Язык установлен: Русский.",
        }
        confirmation = defaults.get(lang_code, "✅ Language updated.")

    await callback.message.edit_text(confirmation)
    await callback.answer()

    # Reload user object with new language (for dashboard)
    if user:
        try:
            async with get_session(silent=True) as session:
                from app.database.models.user import User as UserModel
                refreshed = await session.get(UserModel, user.id)
                if refreshed:
                    user = refreshed
        except Exception:
            logger.exception("Failed to refresh user after language change")

        # Determine the user's roles from their affiliation
        roles: List[str] = []
        if user.supplier_id:
            roles = ["supplier_owner"]
        elif user.company_id:
            roles = ["company_owner"]

        if roles:
            # Existing user: show their dashboard in the selected language
            await show_dashboard_for_user(
                callback.message, user, roles, lang=lang_code
            )
        else:
            # NEW user: show the registration menu
            await show_registration_menu(callback.message, lang=lang_code)
    else:
        # No user record yet (edge case) — show registration menu
        await show_registration_menu(callback.message, lang=lang_code)


# ============================================================================
# Deep Link Processing
# ============================================================================
async def process_deep_link_invite(message: Message, token: str, user: Optional[User]):
    """Process deep link invite like t.me/bot?start=inv_<48_hex_chars>"""
    logger.info(
        "Deep link processing | token_prefix=%s | tg_id=%s",
        token[:8],
        message.from_user.id,
    )

    is_valid, invitation, error_msg = await invite_service.validate_invitation(token)

    if not is_valid:
        await message.answer(
            f"❌ {error_msg}\n\n"
            f"استخدم /start للبدء من جديد أو طلب رابط دعوة جديد من مديرك."
        )
        logger.warning(
            "Invalid deep link token | tg_id=%s | error=%s",
            message.from_user.id,
            error_msg,
        )
        return

    if not user:
        user = await user_service.get_or_create_user(
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name or "Unknown",
            language=message.from_user.language_code or "ar",
        )

    if not user:
        await message.answer("❌ تعذر إنشاء حسابك. حاول لاحقاً.")
        return

    success, invitation, accept_error = await invite_service.accept_invitation(
        token=token,
        user_id=user.id,
    )

    if not success:
        await message.answer(f"❌ {accept_error}")
        return

    from app.database.models.role import Role
    from sqlalchemy import select

    async with get_session(silent=True) as session:
        stmt = select(Role).where(Role.id == invitation.role_id)
        result = await session.execute(stmt)
        role = result.scalar_one()

        await user_service.assign_role(
            user_id=user.id,
            role_code=role.code,
            granted_by=None,
        )

        company = await company_service.get_company_by_id(invitation.company_id)
        company_name = company.name if company else "الشركة"

        # AuditLog for invite acceptance
        audit = AuditLog(
            entity_table="company_invitations",
            entity_id=invitation.id,
            action=AuditAction.STATUS_CHANGE,
            actor_user_id=user.id,
            company_id=invitation.company_id,
            new_data={
                "action": "accept_invitation",
                "role_code": role.code,
                "channel": "inv_",
            },
        )
        session.add(audit)
        await session.commit()

    role_emoji, role_text = get_primary_role_display(
        [role.code], lang=user.language or "ar"
    )

    welcome_text = (
        f"🎉 <b>تم الانضمام بنجاح عبر رابط الدعوة!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏢 الشركة: <b>{company_name}</b>\n"
        f"👤 دورك: {role_emoji} <b>{role_text}</b>\n\n"
        f"👋 مرحباً بك {message.from_user.full_name}!\n"
        f"استخدم /start للوصول إلى القائمة الرئيسية."
    )

    await message.answer(welcome_text, parse_mode="HTML")
    await show_dashboard_for_user(
        message, user, [role.code], lang=user.language or "ar"
    )

    logger.info(
        "User joined company via deep link | tg_id=%s | company=%s | role=%s",
        message.from_user.id,
        company_name,
        role.code,
    )


async def process_employee_invitation(message: Message, token: str, user: Optional[User]):
    """Process employee invitation deep link (emp_<token>)."""
    logger.info(
        "Employee invitation processing | token_prefix=%s | tg_id=%s",
        token[:8],
        message.from_user.id,
    )

    is_valid, invitation, error_msg = await invite_service.validate_invitation(token)

    if not is_valid:
        await message.answer(
            f"❌ {error_msg}\n\n"
            f"الرابط غير صالح أو انتهى. يرجى طلب رابط جديد من مالك الشركة."
        )
        logger.warning(
            "Invalid employee token | tg_id=%s | error=%s",
            message.from_user.id,
            error_msg,
        )
        return

    if invitation.accepted_by is not None:
        await message.answer(
            "⚠️ <b>رابط الدعوة مستخدم بالفعل.</b>\n\n"
            "هذا الرابط صالح للاستخدام مرة واحدة فقط.\n"
            "يرجى طلب رابط دعوة جديد من الشركة.",
            parse_mode="HTML",
        )
        return

    if not user:
        user = await user_service.get_or_create_user(
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name or "Unknown",
            language=message.from_user.language_code or "ar",
        )

    if not user:
        await message.answer("❌ تعذر إنشاء حسابك. حاول لاحقاً.")
        return

    success, invitation, accept_error = await invite_service.accept_invitation(
        token=token,
        user_id=user.id,
    )

    if not success:
        await message.answer(f"❌ {accept_error}")
        return

    from app.database.models.role import Role
    from sqlalchemy import select

    async with get_session(silent=True) as session:
        stmt = select(Role).where(Role.id == invitation.role_id)
        result = await session.execute(stmt)
        role = result.scalar_one()

        await user_service.assign_role(
            user_id=user.id,
            role_code=role.code,
            granted_by=None,
        )

        company = await company_service.get_company_by_id(invitation.company_id)
        company_name = company.name if company else "الشركة"

        # AuditLog for invite acceptance
        audit = AuditLog(
            entity_table="company_invitations",
            entity_id=invitation.id,
            action=AuditAction.STATUS_CHANGE,
            actor_user_id=user.id,
            company_id=invitation.company_id,
            new_data={
                "action": "accept_employee_invitation",
                "role_code": role.code,
                "channel": "emp_",
            },
        )
        session.add(audit)
        await session.commit()

    role_emoji, role_text = get_primary_role_display(
        [role.code], lang=user.language or "ar"
    )

    welcome_text = (
        f"🎉 <b>مرحباً بك في فريق العمل!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏢 الشركة: <b>{company_name}</b>\n"
        f"👤 دورك الجديد: {role_emoji} <b>{role_text}</b>\n\n"
        f"تم تسجيل دخولك تلقائياً. يمكنك الآن استخدام البوت مباشرة."
    )

    await message.answer(welcome_text, parse_mode="HTML")
    await show_dashboard_for_user(
        message, user, [role.code], lang=user.language or "ar"
    )

    logger.info(
        "User joined company via employee link | tg_id=%s | company=%s | role=%s",
        message.from_user.id,
        company_name,
        role.code,
    )


async def process_supplier_invitation(message: Message, token: str, user: Optional[User]):
    """Process supplier invitation deep link (sup_<token>)."""
    logger.info(
        "Supplier invitation processing | token_prefix=%s | tg_id=%s",
        token[:8],
        message.from_user.id,
    )

    is_valid, invitation, error_msg = await invite_service.validate_invitation(token)

    if not is_valid:
        await message.answer(
            f"❌ {error_msg}\n\nاستخدم /start للبدء من جديد."
        )
        logger.warning(
            "Invalid supplier token | tg_id=%s | error=%s",
            message.from_user.id,
            error_msg,
        )
        return

    if invitation.accepted_by is not None:
        await message.answer(
            "⚠️ <b>رابط الدعوة مستخدم بالفعل.</b>\n\n"
            "هذا الرابط صالح للاستخدام مرة واحدة فقط.\n"
            "يرجى طلب رابط دعوة جديد من الشركة.",
            parse_mode="HTML",
        )
        return

    if not user:
        user = await user_service.get_or_create_user(
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name or "Unknown",
            language=message.from_user.language_code or "ar",
        )

    if not user:
        await message.answer("❌ تعذر إنشاء حسابك. حاول لاحقاً.")
        return

    inviting_company_id = invitation.company_id

    from app.database.models.company import Company
    from app.database.models.supplier_relation import SupplierRelation
    from sqlalchemy import select
    from datetime import datetime, timezone

    company_name = "الشركة"
    # RLS-enforced session for writes (was silent=True — security fix)
    async with get_session(company_id=inviting_company_id) as session:
        stmt = select(Company).where(Company.id == inviting_company_id)
        result = await session.execute(stmt)
        company = result.scalar_one_or_none()
        if company:
            company_name = company.name

        invitation.accepted_by = user.id
        invitation.accepted_at = datetime.now(timezone.utc)

        # AuditLog for supplier invitation acceptance
        audit = AuditLog(
            entity_table="company_invitations",
            entity_id=invitation.id,
            action=AuditAction.STATUS_CHANGE,
            actor_user_id=user.id,
            company_id=inviting_company_id,
            new_data={
                "action": "accept_supplier_invitation",
                "channel": "sup_",
                "supplier_id": user.supplier_id,
            },
        )
        session.add(audit)

        await session.commit()

        logger.info(
            "Supplier invitation accepted | user_id=%s | company_id=%s",
            user.id,
            inviting_company_id,
        )

        if user.supplier_id:
            stmt_rel = select(SupplierRelation).where(
                SupplierRelation.company_id == inviting_company_id,
                SupplierRelation.supplier_id == user.supplier_id,
            )
            existing_rel = (await session.execute(stmt_rel)).scalar_one_or_none()

            if not existing_rel:
                new_rel = SupplierRelation(
                    company_id=inviting_company_id,
                    supplier_id=user.supplier_id,
                    status="pending",
                )
                session.add(new_rel)
                await session.commit()

                logger.info(
                    "Supplier relation created | company_id=%s | supplier_id=%s",
                    inviting_company_id,
                    user.supplier_id,
                )
        else:
            logger.info(
                "New user needs supplier registration | user_id=%s | pending_company_id=%s",
                user.id,
                inviting_company_id,
            )

    welcome_text = (
        f"🎉 <b>تم قبول دعوة الشركة بنجاح!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏢 الشركة الداعية: <b>{company_name}</b>\n"
        f"👤 دورك: 🏭 <b>مالك المورد</b>\n\n"
    )

    if user.supplier_id:
        welcome_text += (
            f"✅ تم ربط حسابك بالشركة كـ <b>مورد معلق</b>.\n"
            f"ستظهر العلاقة في قائمة الموردين بعد موافقة المورد.\n\n"
            f"👋 مرحباً بك {message.from_user.full_name}!\n"
            f"استخدم /start للوصول إلى لوحة تحكم المورد."
        )

        await message.answer(welcome_text, parse_mode="HTML")

        roles_list = ["supplier_owner"]
        keyboard = get_keyboard_by_roles(roles_list)
        if keyboard:
            await message.answer(
                "🏭 <b>لوحة تحكم المورد</b>",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    else:
        welcome_text += (
            f"📝 <b>الخطوة التالية:</b> سجّل كمورد جديد لإكمال الربط.\n\n"
            f"اضغط على الزر أدناه للبدء:"
        )

        registration_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🏭 تسجيل كمورد جديد",
                callback_data=f"register:supplier_from_invite:{inviting_company_id}",
            )]
        ])

        await message.answer(
            welcome_text,
            reply_markup=registration_keyboard,
            parse_mode="HTML",
        )
        return

    logger.info(
        "Supplier invitation flow complete | tg_id=%s | company=%s",
        message.from_user.id,
        company_name,
    )


# ============================================================================
# Command: /start
# ============================================================================
@router.message(Command("start"))
async def cmd_start(
    message: Message,
    command: CommandObject,
    user: Optional[User],
    roles: List[str],
    is_super_admin: bool,
    company_id: Optional[int],
    supplier_id: Optional[int],
    lang: str = "ar",
):
    """Handle /start command and deep links."""
    logger.info(
        "START | tg_id=%s | user_id=%s | roles=%s | super_admin=%s | lang=%s",
        message.from_user.id,
        user.id if user else "None",
        roles,
        is_super_admin,
        lang,
    )

    # SAFETY NET 1: Reload roles from DB if empty
    if user and not roles:
        try:
            from app.database.models.role import Role
            from app.database.models.user_role import UserRole
            from sqlalchemy import select

            async with get_session(silent=True) as session:
                stmt = (
                    select(Role.code)
                    .join(UserRole, UserRole.role_id == Role.id)
                    .where(UserRole.user_id == user.id)
                )
                result = await session.execute(stmt)
                db_roles = [row[0] for row in result.all()]

                if db_roles:
                    logger.info(
                        "SAFETY NET: Reloaded roles from DB | user_id=%s | roles=%s",
                        user.id,
                        db_roles,
                    )
                    roles = db_roles
                    if "super_admin" in roles:
                        is_super_admin = True
        except Exception:
            logger.exception("Failed to reload roles in safety net")

    # SAFETY NET 2: Infer role from existing IDs (Pending Approval State)
    if user and not roles:
        if user.supplier_id:
            roles = ["supplier_owner"]
            supplier_id = user.supplier_id
            logger.info(
                "Inferred supplier_owner role for pending user | user_id=%s",
                user.id,
            )
        elif user.company_id:
            roles = ["company_owner"]
            company_id = user.company_id
            logger.info(
                "Inferred company_owner role for pending user | user_id=%s",
                user.id,
            )

    # PRIORITY 0: Deep Links
    if command.args and command.args.startswith("sup_"):
        token = command.args.replace("sup_", "", 1)
        await process_supplier_invitation(message, token, user)
        return

    if command.args and command.args.startswith("emp_"):
        token = command.args.replace("emp_", "", 1)
        await process_employee_invitation(message, token, user)
        return

    if command.args and command.args.startswith("inv_"):
        token = command.args.replace("inv_", "", 1)
        await process_deep_link_invite(message, token, user)
        return

    # PRIORITY 1: Super Admin
    if is_super_admin or "super_admin" in roles:
        logger.info("Routing to Super Admin Panel")
        await show_dashboard_for_user(message, user, roles, lang=lang)
        return

    # PRIORITY 2: Registered User (Company or Supplier)
    if user and roles:
        if permissions.is_buyer_side(roles):
            logger.info("Routing to Company Dashboard")
        elif permissions.is_supplier_side(roles):
            logger.info("Routing to Supplier Dashboard")

        await show_dashboard_for_user(message, user, roles, lang=lang)
        return

    # PRIORITY 3: New User — show language selection first
    logger.info("Routing to Language Selection (new user)")

    await message.answer(
        "🌍 <b>اختر لغتك المفضلة / Choose your language / Выберите язык</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "سيتم حفظ اختيارك، ويمكنك تغييره لاحقاً من الإعدادات.",
        reply_markup=build_language_keyboard(),
        parse_mode="HTML",
    )


# ============================================================================
# Command: /lang — change language anytime
# ============================================================================
@router.message(Command("lang"))
async def cmd_lang(message: Message, lang: str = "ar"):
    """Allow the user to change their language at any time."""
    logger.info("Language command | tg_id=%s", message.from_user.id)

    await message.answer(
        "🌍 <b>اختر لغتك المفضلة / Choose your language / Выберите язык</b>",
        reply_markup=build_language_keyboard(),
        parse_mode="HTML",
    )


# ============================================================================
# Command: /help
# ============================================================================
@router.message(Command("help"))
async def cmd_help(message: Message, roles: List[str], lang: str = "ar"):
    """Handle /help command - show help information."""
    logger.info("HELP | tg_id=%s | lang=%s", message.from_user.id, lang)

    help_text = t("help_text", lang=lang)
    if help_text == "help_text":
        help_text = (
            "❓ <b>المساعدة والدعم</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 <b>كيف تستخدم البوت:</b>\n"
            "• استخدم الأزرار في القائمة الرئيسية للتنقل\n"
            "• اضغط على '🏠 الرئيسية' للعودة للقائمة الرئيسية\n"
            "• اضغط على '🚀 ابدأ من جديد' لإعادة تعيين المحادثة\n\n"
            "📋 <b>الأوامر المتاحة:</b>\n"
            "• /start - البدء من جديد\n"
            "• /help - عرض هذه المساعدة\n"
            "• /lang - تغيير اللغة\n"
            "• /admin - لوحة التحكم (للمدراء فقط)\n\n"
            "📞 <b>الدعم الفني:</b>\n"
            "إذا واجهت أي مشكلة، يرجى التواصل مع مدير النظام.\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "<i>💡 نصيحة: تأكد من أن جميع الأزرار تعمل بشكل صحيح.</i>"
        )

    await message.answer(help_text, parse_mode="HTML")


# ============================================================================
# Button: Home
# ============================================================================
@router.message(F.text == "🏠 الرئيسية")
async def btn_main_menu(
    message: Message,
    user: Optional[User],
    roles: List[str],
    is_super_admin: bool,
    lang: str = "ar",
):
    """Handle Home button click."""
    logger.info("MENU_BTN | tg_id=%s | action=home", message.from_user.id)

    if not user:
        await message.answer("⚠️ يجب عليك تسجيل الدخول أولاً. استخدم /start.")
        return

    await show_dashboard_for_user(message, user, roles, lang=lang)


# ============================================================================
# Button: Restart
# ============================================================================
@router.message(F.text == "🚀 ابدأ من جديد")
async def btn_restart(
    message: Message,
    user: Optional[User],
    roles: List[str],
    is_super_admin: bool,
    lang: str = "ar",
):
    """Handle '🚀 ابدأ من جديد' button - restart the conversation."""
    logger.info("RESTART_BTN | tg_id=%s", message.from_user.id)

    await message.answer(
        "🔄 <b>إعادة التشغيل</b>\n\n"
        "تم إعادة تعيين المحادثة بنجاح.\n"
        "يمكنك البدء من جديد باستخدام الأزرار أدناه.",
        parse_mode="HTML",
    )

    if user:
        await show_dashboard_for_user(message, user, roles, lang=lang)


# ============================================================================
# Button: Help
# ============================================================================
@router.message(F.text == "❓ مساعدة")
async def btn_help(message: Message, roles: List[str], lang: str = "ar"):
    """Handle '❓ مساعدة' button - show help information."""
    logger.info("HELP_BTN | tg_id=%s | lang=%s", message.from_user.id, lang)

    help_text = t("help_text", lang=lang)
    if help_text == "help_text":
        help_text = (
            "❓ <b>المساعدة والدعم</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 <b>كيف تستخدم البوت:</b>\n"
            "• استخدم الأزرار في القائمة الرئيسية للتنقل\n"
            "• اضغط على '🏠 الرئيسية' للعودة للقائمة الرئيسية\n"
            "• اضغط على '🚀 ابدأ من جديد' لإعادة تعيين المحادثة\n\n"
            "📋 <b>الأوامر المتاحة:</b>\n"
            "• /start - البدء من جديد\n"
            "• /help - عرض هذه المساعدة\n"
            "• /lang - تغيير اللغة\n"
            "• /admin - لوحة التحكم (للمدراء فقط)\n\n"
            "📞 <b>الدعم الفني:</b>\n"
            "إذا واجهت أي مشكلة، يرجى التواصل مع مدير النظام.\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "<i>💡 نصيحة: تأكد من أن جميع الأزرار تعمل بشكل صحيح.</i>"
        )

    await message.answer(help_text, parse_mode="HTML")
    
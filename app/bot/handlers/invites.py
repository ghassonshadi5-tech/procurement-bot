"""
Invites handlers - manages employee invitations for company owners.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

KEY CHANGES:
- Uses CompanyInvitation tokens (48 hex chars) instead of RoleInviteCode strings.
- Fully async using invite_service and get_session().
- RBAC enforcement via permissions module.
- Fetches roles from DB instead of hardcoded INVITABLE_ROLES.

CHANGE LOG:
- Added Optional[User] type hints for `user` parameters.
- Added defensive `if not user` check in handle_generate_invite.
- Moved `from datetime import datetime, timezone` to the top of the file.
"""
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.core.logger import get_logger
from app.config.settings import settings
from app.core import permissions
from app.database.database import get_session
from app.database.models.role import Role
from app.database.models.user import User
from app.services.invite_service import invite_service
from app.services.company_service import company_service
from app.bot.keyboards.main_menu import get_keyboard_by_roles

logger = get_logger(__name__)
router = Router(name="invites")


# ============================================================================
# Helper: Build share buttons for invite token
# ============================================================================
def build_invite_share_buttons(token: str, company_name: str, role_display: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with share/copy buttons for invite token."""
    bot_username = settings.BOT_USERNAME

    if not bot_username:
        logger.warning("BOT_USERNAME not set. Share buttons limited.")
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 نسخ الرمز فقط", callback_data=f"copy_token:{token}")]
        ])

    # Deep link format: /start inv_<token>
    invite_link = f"https://t.me/{bot_username}?start=inv_{token}"

    share_text = (
        f"🎉 تمت دعوتك للانضمام إلى:\n"
        f"🏢 {company_name}\n\n"
        f"👤 دورك في الشركة: {role_display}\n\n"
        f"👇 اضغط على الرابط التالي للتسجيل في البوت:"
    )

    encoded_url = urllib.parse.quote(invite_link, safe='')
    encoded_text = urllib.parse.quote(share_text, safe='')
    share_url = f"https://t.me/share/url?url={encoded_url}&text={encoded_text}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📤 مشاركة مع جهة اتصال",
                url=share_url
            )
        ],
        [
            InlineKeyboardButton(
                text="🔗 نسخ رابط الدعوة",
                callback_data=f"copy_link:{token}"
            ),
            InlineKeyboardButton(
                text="📋 نسخ الرمز فقط",
                callback_data=f"copy_token:{token}"
            )
        ]
    ])


# ============================================================================
# Helper: Get role display name (Arabic)
# ============================================================================
async def get_role_display_ar(role_code: str) -> str:
    """Fetch role Arabic name from DB."""
    async with get_session(silent=True) as session:
        stmt = select(Role).where(Role.code == role_code)
        result = await session.execute(stmt)
        role = result.scalar_one_or_none()
        return role.name_ar if role else role_code


async def get_role_display_ar_by_id(role_id: int) -> str:
    """Fetch role Arabic name by ID."""
    async with get_session(silent=True) as session:
        role = await session.get(Role, role_id)
        return role.name_ar if role else "دور غير معروف"


# ============================================================================
# Button Handlers: Generate Invite for Specific Roles
# ============================================================================
@router.message(F.text == "💼 دعوة مدير")
async def btn_invite_manager(
    message: Message,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
):
    await handle_generate_invite(message, user, roles, company_id, "manager")


@router.message(F.text == "🧮 دعوة محاسب")
async def btn_invite_accountant(
    message: Message,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
):
    await handle_generate_invite(message, user, roles, company_id, "accountant")


@router.message(F.text == "📦 دعوة أمين مستودع")
async def btn_invite_storekeeper(
    message: Message,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
):
    await handle_generate_invite(message, user, roles, company_id, "storekeeper")


@router.message(F.text == "👷 دعوة موظف")
async def btn_invite_requester(
    message: Message,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
):
    await handle_generate_invite(message, user, roles, company_id, "requester")


# ============================================================================
# Core Logic: Generate and Send Invite Token
# ============================================================================
async def handle_generate_invite(
    message: Message,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
    role_code: str,
):
    """Common logic for generating an invite token for any role."""

    role_keyboard = get_keyboard_by_roles(roles)

    # 1. Permission Check (RBAC)
    if not permissions.can_manage_company_users(roles):
        await message.answer(
            "⛔ هذه الوظيفة متاحة لمالكي الشركات فقط.",
            reply_markup=role_keyboard,
        )
        return

    # 2. Defensive check for user
    if not user:
        await message.answer("⛔ خطأ في سياق المستخدم.", reply_markup=role_keyboard)
        return

    if not company_id:
        await message.answer("⛔ خطأ في سياق الشركة.", reply_markup=role_keyboard)
        return

    # 3. Fetch company name
    company_name = "الشركة"
    try:
        company_obj = await company_service.get_company_by_id(company_id)
        if company_obj:
            company_name = company_obj.name
    except Exception:
        logger.exception("Could not fetch company name | company_id=%s", company_id)

    # 4. Get role display name
    role_display = await get_role_display_ar(role_code)

    try:
        # 5. Call Service to Create Invitation (Generates Token in DB)
        invitation = await invite_service.create_invitation(
            company_id=company_id,
            role_code=role_code,
            created_by_user_id=user.id,
        )

        if not invitation:
            await message.answer(
                "❌ فشل توليد رمز الدعوة. تأكد من وجود الدور في النظام.",
                reply_markup=role_keyboard,
            )
            return

        # 6. Format Response Message
        bot_username = settings.BOT_USERNAME or "البوت"
        invite_link = f"https://t.me/{bot_username}?start=inv_{invitation.token}"

        response_text = (
            f"✅ <b>تم توليد رمز دعوة جديد!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏢 الشركة: <b>{company_name}</b>\n"
            f"👤 الدور: <b>{role_display}</b>\n"
            f"🔑 الرمز: <code>{invitation.token}</code>\n\n"
            f"🔗 رابط الدعوة:\n<code>{invite_link}</code>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📢 <b>طرق إرسال الدعوة:</b>\n\n"
            f"📤 <b>الطريقة الأسهل:</b> اضغط على زر \"مشاركة مع جهة اتصال\" "
            f"واختر الشخص من قائمة Telegram.\n\n"
            f"📋 <b>الطريقة اليدوية:</b> انسخ الرابط أعلاه وأرسله للموظف.\n\n"
            f"⚠️ <i>هذا الرمز صالح للاستخدام مرة واحدة فقط ولمدة 7 أيام.</i>"
        )

        # 7. Build share buttons
        share_keyboard = build_invite_share_buttons(invitation.token, company_name, role_display)

        # 8. Send message with inline buttons
        await message.answer(response_text, reply_markup=share_keyboard, parse_mode="HTML")

        # 9. Send a separate message with the role keyboard
        await message.answer("👇 القائمة الرئيسية:", reply_markup=role_keyboard)

        logger.info(
            "Invite token generated | role_code=%s | by_user=%s | company=%s",
            role_code,
            user.id,
            company_name,
        )

    except Exception:
        logger.exception("Error generating invite token | role_code=%s", role_code)
        await message.answer("❌ حدث خطأ أثناء توليد الرمز.", reply_markup=role_keyboard)


# ============================================================================
# Callback: Copy invite link
# ============================================================================
@router.callback_query(F.data.startswith("copy_link:"))
async def callback_copy_link(callback: CallbackQuery):
    """Handle copy link button."""
    token = callback.data.split(":", 1)[1]
    bot_username = settings.BOT_USERNAME

    if not bot_username:
        await callback.answer("⚠️ إعدادات البوت غير مكتملة.", show_alert=True)
        return

    invite_link = f"https://t.me/{bot_username}?start=inv_{token}"

    await callback.answer(
        f"📋 الرابط:\n{invite_link}\n\n💡 اضغط مطولاً على الرابط في الرسالة أعلاه لنسخه.",
        show_alert=True,
        cache_time=10,
    )


# ============================================================================
# Callback: Copy invite token only
# ============================================================================
@router.callback_query(F.data.startswith("copy_token:"))
async def callback_copy_token(callback: CallbackQuery):
    """Handle copy token button."""
    token = callback.data.split(":", 1)[1]

    await callback.answer(
        f"📋 الرمز: {token}\n\n💡 اضغط مطولاً على الرمز في الرسالة أعلاه لنسخه.",
        show_alert=True,
        cache_time=10,
    )


# ============================================================================
# Button Handler: List Pending Invitations
# ============================================================================
@router.message(F.text == "📋 رموز الدعوة")
async def btn_list_invites(
    message: Message,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
):
    """Display all pending (unused, unexpired) invitations for the current company."""

    role_keyboard = get_keyboard_by_roles(roles)

    # 1. Permission Check
    if not permissions.can_manage_company_users(roles):
        await message.answer(
            "⛔ هذه الوظيفة متاحة لمالكي الشركات فقط.",
            reply_markup=role_keyboard,
        )
        return

    if not company_id:
        await message.answer("⛔ خطأ في سياق الشركة.", reply_markup=role_keyboard)
        return

    try:
        # 2. Fetch Pending Invitations via Service
        pending_invitations = await invite_service.get_pending_invitations(company_id)

        if not pending_invitations:
            await message.answer(
                "📭 لا توجد رموز دعوة نشطة حالياً.\n"
                "استخدم أزرار الدعوة لإنشاء رمز جديد.",
                reply_markup=role_keyboard,
            )
            return

        # 3. Format List
        lines = [f"📋 <b>رموز الدعوة النشطة ({len(pending_invitations)}):</b>\n"]

        for i, inv in enumerate(pending_invitations[:10], 1):
            role_display = await get_role_display_ar_by_id(inv.role_id)

            now = datetime.now(timezone.utc)
            if inv.expires_at:
                delta = inv.expires_at - now
                days = delta.days
                hours = delta.seconds // 3600
                time_left = f"{days} يوم" if days > 0 else f"{hours} ساعة"
            else:
                time_left = "غير محدد"

            line = (
                f"<b>{i}.</b> 🔑 <code>{inv.token[:16]}...</code>\n"
                f"   👤 الدور: {role_display}\n"
                f"   ⏳ ينتهي خلال: {time_left}\n"
            )
            lines.append(line)

        if len(pending_invitations) > 10:
            lines.append(f"\n<i>... و {len(pending_invitations) - 10} رموز أخرى.</i>")

        final_text = "\n".join(lines)

        await message.answer(final_text, reply_markup=role_keyboard, parse_mode="HTML")

    except Exception:
        logger.exception("Error listing invites | company_id=%s", company_id)
        await message.answer(
            "❌ حدث خطأ أثناء جلب قائمة الرموز.",
            reply_markup=role_keyboard,
        )
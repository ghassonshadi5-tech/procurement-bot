"""
Company Owner - Employees management handlers.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.
"""
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import secrets

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    CopyTextButton,
)
from sqlalchemy import select, func

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session
from app.database.models.user import User, UserStatus
from app.database.models.company_invitation import CompanyInvitation

from app.bot.handlers.company_owner._helpers import (
    EMPLOYEE_ROLES_FOR_INVITE,
    require_company_owner_or_manager,
    require_buyer_admin_access,
)

logger = get_logger(__name__)
router = Router(name="company_owner_employees")


# ============================================================================
# FSM States
# ============================================================================
class EmployeeInviteFlow(StatesGroup):
    choosing_role = State()
    confirming_invitation = State()


# ============================================================================
# Button: "👥 الموظفين والدعوات"
# ============================================================================
@router.message(F.text == "👥 الموظفين والدعوات")
async def btn_manage_employees(
    message: Message,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle '👥 الموظفين والدعوات' button."""
    if not require_buyer_admin_access(roles):
        await message.answer("⛔ غير مصرح لك بالوصول لهذه الوظيفة.")
        return

    if not user or not company_id:
        await message.answer("⚠️ لم يتم العثور على شركتك.")
        return

    can_manage = permissions.can_manage_company_users(roles)

    logger.info(
        "COMPANY_OWNER | managing employees | tg_id=%s | company_id=%s",
        message.from_user.id,
        company_id,
    )

    try:
        async with get_session(company_id=company_id) as session:
            stmt_users = (
                select(func.count(User.id))
                .where(
                    User.company_id == company_id,
                    User.status == UserStatus.active
                )
            )
            result_users = await session.execute(stmt_users)
            employee_count = result_users.scalar() or 0

            stmt_invites = (
                select(func.count(CompanyInvitation.id))
                .where(
                    CompanyInvitation.company_id == company_id,
                    CompanyInvitation.accepted_by.is_(None)
                )
            )
            result_invites = await session.execute(stmt_invites)
            pending_invites = result_invites.scalar() or 0

        response = (
            f"👥 <b>إدارة الموظفين والدعوات</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>الموظفون النشطون:</b> {employee_count}\n"
            f"📨 <b>الدعوات المعلقة:</b> {pending_invites}\n\n"
        )

        if can_manage:
            response += (
                f"يمكنك من هنا:\n"
                f"• دعوة موظفين جدد للانضمام لشركتك\n"
                f"• عرض قائمة الموظفين الحاليين\n"
                f"• إلغاء الدعوات المعلقة"
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ إنشاء دعوة جديدة", callback_data="co_create_invite"),
                    InlineKeyboardButton(text="📋 عرض الموظفين", callback_data="co_list_employees")
                ],
                [
                    InlineKeyboardButton(text="📨 الدعوات المعلقة", callback_data="co_list_pending_invites")
                ]
            ])
        else:
            response += "<i>💡 لديك صلاحية العرض فقط.</i>"

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 عرض الموظفين", callback_data="co_list_employees")]
            ])

        await message.answer(response, reply_markup=keyboard, parse_mode="HTML")

    except Exception:
        logger.exception("Error fetching employee data")
        await message.answer("❌ عذراً، حدث خطأ أثناء جلب بيانات الموظفين.")


# ============================================================================
# Callback: Create Invite — Show Role Selection
# ============================================================================
@router.callback_query(F.data == "co_create_invite")
async def callback_create_invite(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle create invite button - Show Role Selection."""
    if not permissions.can_manage_company_users(roles):
        await callback.answer("⛔ هذه الوظيفة متاحة لمالك الشركة فقط.", show_alert=True)
        return

    if not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    buttons = []
    for code, label in EMPLOYEE_ROLES_FOR_INVITE:
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"emp_sel_role:{code}")])

    buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="emp_cancel_invite")])

    await callback.message.edit_text(
        "👥 <b>إنشاء دعوة موظف</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "اختر الدور الوظيفي للموظف الجديد:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(company_id=company_id)
    await state.set_state(EmployeeInviteFlow.choosing_role)
    await callback.answer()


# ============================================================================
# Callback: Select Employee Role
# ============================================================================
@router.callback_query(EmployeeInviteFlow.choosing_role, F.data.startswith("emp_sel_role:"))
async def cb_select_emp_role(callback: CallbackQuery, state: FSMContext):
    """Handle employee role selection."""
    role_code = callback.data.split(":", 1)[1]
    await state.update_data(role_code=role_code)

    # Find the display label for this role
    role_label = role_code
    for code, label in EMPLOYEE_ROLES_FOR_INVITE:
        if code == role_code:
            role_label = label
            break

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ توليد الرابط", callback_data="emp_confirm_gen")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="emp_cancel_invite")]
    ])

    await callback.message.edit_text(
        f"👤 الدور المختار: <b>{role_label}</b>\n\n"
        "هل تريد توليد رابط الدعوة لهذا الدور؟",
        reply_markup=confirm_kb,
        parse_mode="HTML"
    )
    await state.set_state(EmployeeInviteFlow.confirming_invitation)
    await callback.answer()


# ============================================================================
# Callback: Generate Employee Invite Link
# ============================================================================
@router.callback_query(EmployeeInviteFlow.confirming_invitation, F.data == "emp_confirm_gen")
async def cb_generate_emp_link(
    callback: CallbackQuery,
    state: FSMContext,
    user,
):
    """Generate employee invitation link."""
    data = await state.get_data()
    company_id = data.get("company_id")
    role_code = data.get("role_code")
    await state.clear()

    if not company_id or not role_code:
        await callback.answer("❌ خطأ في البيانات.", show_alert=True)
        return

    try:
        from app.database.models.role import Role
        from app.database.models.company import Company
        from app.config.settings import settings

        token = secrets.token_hex(24)

        async with get_session(company_id=company_id) as session:
            stmt_role = select(Role).where(Role.code == role_code)
            res_role = await session.execute(stmt_role)
            role = res_role.scalar_one_or_none()

            if not role:
                await callback.answer("❌ الدور غير معروف.", show_alert=True)
                return

            inv = CompanyInvitation(
                company_id=company_id,
                role_id=role.id,
                token=token,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=48)
            )
            session.add(inv)

            comp_stmt = select(Company).where(Company.id == company_id)
            comp_res = await session.execute(comp_stmt)
            company_obj = comp_res.scalar_one_or_none()
            company_name = company_obj.name if company_obj else "الشركة"

            await session.commit()

        bot_username = settings.BOT_USERNAME
        invite_link = f"https://t.me/{bot_username}?start=emp_{token}"

        action_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 نسخ الرابط",
                    copy_text=CopyTextButton(text=invite_link)
                ),
                InlineKeyboardButton(
                    text="📤 مشاركة",
                    url=f"https://t.me/share/url?url={invite_link}&text=دعوة للانضمام لفريق {company_name}"
                )
            ],
            [InlineKeyboardButton(text="🔙 رجوع للقائمة", callback_data="back_to_main")]
        ])

        await callback.message.edit_text(
            f"🔗 <b>تم إنشاء رابط الدعوة!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏢 الشركة: <b>{company_name}</b>\n"
            f"👤 الدور: <b>{role_code}</b>\n"
            f"⏰ الصلاحية: <b>48 ساعة</b>\n\n"
            f"🔗 الرابط:\n<code>{invite_link}</code>",
            reply_markup=action_keyboard,
            parse_mode="HTML"
        )
        await callback.answer("✅ تم الإنشاء", show_alert=False)

    except Exception:
        logger.exception("Error generating employee invite")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# Callback: Cancel Employee Invite
# ============================================================================
@router.callback_query(F.data == "emp_cancel_invite")
async def cb_cancel_emp_invite(callback: CallbackQuery, state: FSMContext):
    """Cancel employee invite flow."""
    await state.clear()
    await callback.message.edit_text("❌ تم إلغاء الدعوة.", parse_mode="HTML")
    await callback.answer()


# ============================================================================
# Callback: List Employees (Placeholder)
# ============================================================================
@router.callback_query(F.data == "co_list_employees")
async def callback_list_employees(
    callback: CallbackQuery,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle list employees button (placeholder)."""
    if not require_company_owner_or_manager(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    await callback.message.answer(
        "📋 <b>قائمة الموظفين</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "هذه الوظيفة قيد التطوير حالياً.\n"
        "سيتم إضافتها في التحديثات القادمة."
    )
    await callback.answer()


# ============================================================================
# Callback: List Pending Invites (Placeholder)
# ============================================================================
@router.callback_query(F.data == "co_list_pending_invites")
async def callback_list_pending_invites(
    callback: CallbackQuery,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle list pending invites button (placeholder)."""
    if not permissions.can_manage_company_users(roles):
        await callback.answer("⛔ هذه الوظيفة متاحة لمالك الشركة فقط.", show_alert=True)
        return

    await callback.message.answer(
        "📨 <b>الدعوات المعلقة</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "هذه الوظيفة قيد التطوير حالياً.\n"
        "سيتم إضافتها في التحديثات القادمة."
    )
    await callback.answer()
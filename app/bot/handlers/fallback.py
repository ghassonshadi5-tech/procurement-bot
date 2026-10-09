"""
Fallback handlers - Catches any message not handled by other routers.
This router MUST be registered LAST in main.py.

Handles:
- Unknown text messages
- Restart button ("🚀 ابدأ من جديد")
- Help button ("❓ مساعدة")
- Buttons for features still under development
  (Supplier-side and Accountant/Storekeeper buttons)
- Smart fallback for Accountant buttons if primary router misses them.

CHANGE LOG:
- Changed `F.text & ~F.text.startswith("/")` to `F.text, ~F.text.startswith("/")`
  for clarity.
- Replaced `return` with `raise SkipHandler()` in btn_safe_fallback_readonly
  to properly allow fall-through.
- Replaced `silent=True` with `company_id=company_id` in safe fallback
  queries to enforce RLS (security fix).
"""
from typing import List, Optional
from aiogram import Router, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.core.logger import get_logger
from app.database.models.user import User
from app.core import permissions

logger = get_logger(__name__)
router = Router(name="fallback")


# ============================================================================
# Helper: Show registration menu for new users
# ============================================================================
async def show_registration_menu(message: Message):
    """Show the registration menu for new users."""
    registration_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 إنشاء شركة جديدة", callback_data="register:create_company")],
        [InlineKeyboardButton(text="🔑 الانضمام لشركة موجودة", callback_data="register:join_company")],
        [InlineKeyboardButton(text="🏭 تسجيل كمورد", callback_data="register:supplier")],
    ])

    await message.answer(
        f"👋 مرحباً بك <b>{message.from_user.full_name}</b>!\n\n"
        f"أنت جديد في نظام إدارة المشتريات.\n"
        f"اختر أحد الخيارات التالية للبدء:",
        reply_markup=registration_keyboard,
        parse_mode="HTML"
    )


# ============================================================================
# Button: Restart / Start Over
# ============================================================================
@router.message(F.text == "🚀 ابدأ من جديد")
async def btn_restart(message: Message, user: Optional[User], roles: List[str]):
    """Handle restart button - shows registration menu for new users."""
    logger.info("RESTART | tg_id=%s", message.from_user.id)

    if user and roles:
        from app.bot.handlers.start import show_dashboard_for_user
        await show_dashboard_for_user(message, user, roles)
    else:
        await show_registration_menu(message)


# ============================================================================
# Button: Help (❓ مساعدة)
# ============================================================================
@router.message(F.text == "❓ مساعدة")
async def btn_help(message: Message, user: Optional[User], roles: List[str]):
    """Handle help button - shows help message."""
    logger.info("HELP | tg_id=%s", message.from_user.id)

    if user and roles:
        from app.bot.keyboards.main_menu import get_keyboard_by_roles
        keyboard = get_keyboard_by_roles(roles)
    else:
        keyboard = None

    help_text = (
        "📖 <b>مساعدة نظام إدارة المشتريات</b>\n\n"
        "📋 <b>الأوامر المتاحة:</b>\n"
        "/start - القائمة الرئيسية\n"
        "/new_request - إنشاء طلب شراء جديد\n"
        "/my_requests - عرض طلباتك\n"
        "/pending_requests - مراجعة الطلبات المعلقة (للمديرين)\n"
        "/manage_request - إدارة حالة طلب (للمديرين)\n\n"
        "💡 <b>نصيحة:</b> استخدم الأزرار في القائمة الرئيسية للتنقل بسهولة."
    )

    await message.answer(help_text, reply_markup=keyboard, parse_mode="HTML")


# ============================================================================
# Under Development Buttons
# ============================================================================
UNDER_DEVELOPMENT_BUTTONS = [
    # Supplier side (still under development)
    "📦 الكتالوج والأسعار",
    "📨 طلبات العروض الواردة (RFQs)",
    "📄 عروضي المقدمة",
    "🏢 الشركات المرتبطة",
    "👥 موظفو المورد",
    "⚙️ إعدادات المورد",
    "📦 الكتالوج (للاطلاع)",
    # Storekeeper specific (if not fully implemented yet)
    "📦 استلام البضائع",
]


@router.message(F.text.in_(UNDER_DEVELOPMENT_BUTTONS))
async def btn_under_development(message: Message):
    """Handle buttons for features under development."""
    logger.info(
        "UNDER_DEV | tg_id=%s | text=%s",
        message.from_user.id,
        message.text,
    )

    await message.answer(
        "🚧 <b>هذه الميزة قيد التطوير</b>\n\n"
        "سيتم تفعيلها قريباً في التحديثات القادمة.\n"
        "شكراً لصبرك!",
        parse_mode="HTML"
    )


# ============================================================================
# Smart Fallback for Accountant/Manager Read-Only Views
# ============================================================================
# NOTE: This is a safety net that duplicates a small part of
# company_owner.py logic. It exists because the company_owner router
# does not yet use explicit permission filters on those buttons, so
# Accountants sometimes fall through to this router.
#
# Once company_owner.py is refactored, this fallback can be removed.
# ============================================================================
ACCOUNTANT_SAFE_BUTTONS = [
    "📊 التقارير",
    "📋 طلبات الشراء",
    "📨 طلبات عروض الأسعار (RFQ)",
    "🏪 إدارة الموردين",
]


@router.message(F.text.in_(ACCOUNTANT_SAFE_BUTTONS))
async def btn_safe_fallback_readonly(
    message: Message,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
):
    """
    Safety net for read-only views for Company Side users.
    Only triggers if the main router didn't handle it.
    """
    logger.info(
        "DEBUG_FALLBACK | caught | tg_id=%s | text=%s",
        message.from_user.id,
        message.text,
    )

    # Check if user is actually allowed (Owner, Manager, or Accountant)
    if not user or not company_id:
        # Fall through to next handler
        raise SkipHandler()

    is_allowed = (
        permissions.is_company_owner(roles) or
        permissions.is_manager(roles) or
        permissions.is_accountant(roles)
    )

    if not is_allowed:
        # Not our business, fall through
        raise SkipHandler()

    logger.info(
        "SAFE_FALLBACK | routing | user_id=%s | roles=%s | text=%s",
        user.id,
        roles,
        message.text,
    )

    try:
        if message.text == "📊 التقارير":
            await _safe_fallback_reports(message, company_id)
            return

        elif message.text == "📋 طلبات الشراء":
            await _safe_fallback_purchase_requests(message, company_id)
            return

        # RFQ and Suppliers fall through for now
        raise SkipHandler()

    except SkipHandler:
        raise
    except Exception:
        logger.exception("Error in Safe Fallback")
        await message.answer("❌ حدث خطأ أثناء جلب البيانات.")
        return


async def _safe_fallback_reports(message: Message, company_id: int):
    """Lightweight company reports (safety net for Accountants)."""
    from app.database.models.purchase_request import PurchaseRequest
    from app.database.models.rfq import Rfq
    from app.database.models.supplier_relation import SupplierRelation
    from sqlalchemy import select, func
    from app.database.database import get_session
    from datetime import datetime

    # RLS-enforced session (was silent=True — security fix)
    async with get_session(company_id=company_id) as session:
        stmt_pr = (
            select(PurchaseRequest.status, func.count(PurchaseRequest.id))
            .where(PurchaseRequest.company_id == company_id)
            .group_by(PurchaseRequest.status)
        )
        pr_stats = (await session.execute(stmt_pr)).all()

        stmt_rfq = (
            select(Rfq.status, func.count(Rfq.id))
            .where(Rfq.company_id == company_id)
            .group_by(Rfq.status)
        )
        rfq_stats = (await session.execute(stmt_rfq)).all()

        stmt_suppliers = (
            select(func.count(SupplierRelation.id))
            .where(
                SupplierRelation.company_id == company_id,
                SupplierRelation.status == "active",
            )
        )
        active_suppliers = (await session.execute(stmt_suppliers)).scalar() or 0

    response = (
        f"📊 <b>تقارير الشركة</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛒 <b>طلبات الشراء:</b>\n"
    )

    for status, count in pr_stats:
        status_val = status.value if hasattr(status, "value") else str(status)
        response += f"   • {status_val}: <b>{count}</b>\n"

    response += f"\n📨 <b>طلبات عروض الأسعار (RFQ):</b>\n"

    for status, count in rfq_stats:
        status_val = status.value if hasattr(status, "value") else str(status)
        response += f"   • {status_val}: <b>{count}</b>\n"

    response += (
        f"\n🏪 <b>الموردون النشطون:</b> {active_suppliers}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<i>📅 تم إنشاء التقرير في: {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 تصدير إلى Excel", callback_data="co_export_report_excel")]
    ])

    await message.answer(response, reply_markup=kb, parse_mode="HTML")


async def _safe_fallback_purchase_requests(message: Message, company_id: int):
    """Lightweight purchase-requests list (safety net for Accountants)."""
    from app.database.models.purchase_request import PurchaseRequest
    from sqlalchemy import select
    from app.database.database import get_session

    # RLS-enforced session (was silent=True — security fix)
    async with get_session(company_id=company_id) as session:
        stmt = (
            select(PurchaseRequest)
            .where(PurchaseRequest.company_id == company_id)
            .order_by(PurchaseRequest.created_at.desc())
            .limit(5)
        )
        requests = (await session.execute(stmt)).scalars().all()

    if not requests:
        await message.answer(
            "📭 <b>لا توجد طلبات شراء حالياً.</b>",
            parse_mode="HTML",
        )
        return

    text = f"📋 <b>آخر طلبات الشراء ({len(requests)}):</b>\n━━━━━━━━━━━━━━━━━━━\n"
    for i, req in enumerate(requests, 1):
        status = req.status.value if hasattr(req.status, "value") else str(req.status)
        text += f"\n{i}. <b>{req.title}</b>\n   الحالة: {status}\n   🆔 <code>{req.id}</code>"

    await message.answer(text, parse_mode="HTML")


# ============================================================================
# Fallback Handler: Any unknown text message
# MUST be the LAST handler in the entire bot!
# ============================================================================
@router.message(F.text, ~F.text.startswith("/"))
async def fallback_unknown_message(message: Message, user: Optional[User], roles: List[str]):
    """
    Handle any text message that is not a command and doesn't match any button.
    """
    logger.info(
        "FALLBACK | unknown message | tg_id=%s | text=%s",
        message.from_user.id,
        message.text,
    )

    if user and roles:
        from app.bot.keyboards.main_menu import get_keyboard_by_roles
        keyboard = get_keyboard_by_roles(roles)

        await message.answer(
            "⚠️ <b>أمر غير معروف</b>\n\n"
            "الرجاء استخدام الأزرار في القائمة الرئيسية أو كتابة /start للعودة.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    await show_registration_menu(message)
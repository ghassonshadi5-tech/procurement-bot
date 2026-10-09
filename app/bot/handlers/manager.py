"""
Manager handlers - manual request management and state interventions.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Handles:
- /manage_request (or /change_status for backward compatibility)
- Dynamic action menu based on the strict Request Lifecycle State Machine.
- Enforces mandatory reasons for rejections and revision requests.

CHANGE LOG:
- FIXED (critical): process_action_choice now passes the DB User (from
  middleware `data["user"]`) to execute_action, instead of
  callback.from_user (which is an aiogram User whose .id is a Telegram ID,
  not the DB primary key). This ensures audit_log.actor_user_id is correct.
- Added Optional[User] type hints for `user` parameters.
"""
from typing import List, Optional
from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.user import User
from app.database.models.enums import RequestStatus
from app.services.purchase_request_service import purchase_request_service

logger = get_logger(__name__)
router = Router(name="manager")


# ============================================================================
# FSM States
# ============================================================================
class ManageRequestFlow(StatesGroup):
    waiting_for_request_id = State()
    waiting_for_action = State()
    waiting_for_reason = State()


# ============================================================================
# Helpers
# ============================================================================
def get_status_text(status: RequestStatus) -> str:
    """Return Arabic text with emoji for request status."""
    status_map = {
        RequestStatus.draft: "📝 مسودة",
        RequestStatus.submitted: "📨 مُرسل للمراجعة",
        RequestStatus.under_review: "👁️ قيد المراجعة",
        RequestStatus.needs_revision: "🔄 يحتاج تعديل",
        RequestStatus.approved: "✅ معتمد",
        RequestStatus.rejected: "❌ مرفوض",
        RequestStatus.cancelled: "🚫 ملغى",
        RequestStatus.rfq_created: "📢 تم طرحه للمنافسة",
        RequestStatus.awarded: "🏆 تمت الترسية",
        RequestStatus.received: "📦 تم الاستلام",
        RequestStatus.closed: "🏁 مغلق",
        RequestStatus.archived: "🗄️ أرشيف",
    }
    return status_map.get(status, str(status))


def get_allowed_actions(status: RequestStatus) -> dict:
    """
    Return allowed manual actions based on the current state.
    Strictly follows the State Machine defined in the Lifecycle Document.
    """
    if status == RequestStatus.draft:
        return {"submit": "📨 إرسال للمراجعة", "cancel": "🚫 إلغاء الطلب"}
    elif status == RequestStatus.submitted:
        return {
            "review": "👁️ بدء المراجعة",
            "approve": "✅ اعتماد",
            "reject": "❌ رفض",
            "revision": "🔄 طلب تعديل",
            "cancel": "🚫 إلغاء الطلب",
        }
    elif status == RequestStatus.under_review:
        return {
            "approve": "✅ اعتماد",
            "reject": "❌ رفض",
            "revision": "🔄 طلب تعديل",
            "cancel": "🚫 إلغاء الطلب",
        }
    elif status == RequestStatus.needs_revision:
        return {"submit": "📨 إعادة الإرسال", "cancel": "🚫 إلغاء الطلب"}
    elif status == RequestStatus.approved:
        return {"cancel": "🚫 إلغاء الطلب"}

    # Terminal or advanced states (rfq_created, awarded, etc.)
    return {}


# ============================================================================
# Command: Start Management Flow
# ============================================================================
@router.message(Command("manage_request"))
@router.message(Command("change_status"))  # Backward compatibility
@router.message(F.text.in_(["🔄 تعديل حالة طلب", "🔄 إدارة طلب"]))
async def cmd_manage_request(
    message: Message,
    user: Optional[User],
    roles: List[str],
    state: FSMContext,
):
    """Start the manual request management flow."""
    if not user:
        await message.answer("⛔ يجب التسجيل أولاً.")
        return

    if not permissions.can_approve_requests(roles):
        await message.answer("⛔ هذه الوظيفة متاحة للمديرين ومالكي الشركات فقط.")
        return

    await state.clear()
    await message.answer(
        "🔄 <b>إدارة طلب شراء</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "الرجاء إدخال <b>رقم الطلب</b> (الرقم الكامل):\n"
        "(مثال: 142)\n\n"
        "أو اكتب 'إلغاء' للخروج.",
        parse_mode="HTML",
    )
    await state.set_state(ManageRequestFlow.waiting_for_request_id)


# ============================================================================
# FSM Step 1: Receive Request ID
# ============================================================================
@router.message(ManageRequestFlow.waiting_for_request_id)
async def process_request_id(
    message: Message,
    user: User,
    roles: List[str],
    state: FSMContext,
    company_id: Optional[int],
):
    if not message.text:
        await message.answer("❌ الرجاء إدخال رقم صحيح.")
        return

    text = message.text.strip()
    if text == "إلغاء":
        await state.clear()
        await message.answer("✅ تم الإلغاء.")
        return

    if not company_id:
        await state.clear()
        await message.answer("⛔ خطأ في سياق الشركة.")
        return

    try:
        req_id = int(text)
    except ValueError:
        await message.answer("❌ الرجاء إدخال رقم صحيح فقط.")
        return

    req = await purchase_request_service.get_request_by_id(req_id, company_id)
    if not req:
        await message.answer("❌ لم يتم العثور على طلب بهذا الرقم في شركتك.")
        return

    lines = await purchase_request_service.get_request_lines(req_id, company_id)

    actions = get_allowed_actions(req.status)
    if not actions:
        await message.answer(
            f"ℹ️ الطلب <b>{req.title}</b> في حالة <b>{get_status_text(req.status)}</b>.\n"
            f"لا توجد إجراءات يدوية متاحة لهذه الحالة.",
            parse_mode="HTML",
        )
        await state.clear()
        return

    buttons = []
    for action_key, action_text in actions.items():
        buttons.append([
            InlineKeyboardButton(
                text=action_text,
                callback_data=f"manage_action:{req_id}:{action_key}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_manage_flow")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await state.update_data(req_id=req_id, current_status=req.status.value)

    await message.answer(
        f"📋 <b>الطلب:</b> {req.title}\n"
        f"🔢 <b>الرقم:</b> <code>{req.id}</code>\n"
        f"📦 <b>عدد البنود:</b> {len(lines)}\n"
        f"📊 <b>الحالة الحالية:</b> {get_status_text(req.status)}\n\n"
        f"🔄 اختر الإجراء المطلوب:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.set_state(ManageRequestFlow.waiting_for_action)


# ============================================================================
# FSM Step 2: Choose Action
# ============================================================================
@router.callback_query(ManageRequestFlow.waiting_for_action, F.data.startswith("manage_action:"))
async def process_action_choice(
    callback: CallbackQuery,
    state: FSMContext,
    user: Optional[User],
    roles: List[str],
    company_id: Optional[int],
):
    # Permission check (defense in depth — cmd_manage_request already checked)
    if not permissions.can_approve_requests(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    # CRITICAL: We need the DB User (not aiogram user) for audit_log.actor_user_id
    if not user:
        await callback.answer("⚠️ لم يتم التعرف على المستخدم.", show_alert=True)
        return

    parts = callback.data.split(":")
    req_id = int(parts[1])
    action = parts[2]

    await state.update_data(selected_action=action)

    # Actions that REQUIRE a reason/note (Enforced by DB CHECK constraints)
    if action in ["reject", "revision"]:
        action_names = {"reject": "الرفض", "revision": "طلب التعديل"}
        await callback.message.edit_text(
            f"📝 الرجاء إدخال <b>سبب {action_names[action]}</b>:\n"
            f"(هذا السبب سيظهر لمقدم الطلب وهو إلزامي)",
            parse_mode="HTML",
        )
        await state.set_state(ManageRequestFlow.waiting_for_reason)
    else:
        # Execute immediately (submit, review, approve, cancel)
        # FIXED: pass DB User instead of callback.from_user
        await execute_action(
            callback.message,
            user,
            state,
            req_id,
            action,
            None,
            is_callback=True,
            company_id=company_id,
        )

    await callback.answer()


# ============================================================================
# FSM Step 3: Receive Reason (if needed)
# ============================================================================
@router.message(ManageRequestFlow.waiting_for_reason)
async def process_reason(
    message: Message,
    user: User,
    state: FSMContext,
    company_id: Optional[int],
):
    reason = message.text.strip() if message.text else ""
    if not reason:
        await message.answer("❌ الرجاء إدخال نص للسبب.")
        return

    data = await state.get_data()
    req_id = data["req_id"]
    action = data["selected_action"]

    await execute_action(
        message,
        user,
        state,
        req_id,
        action,
        reason,
        is_callback=False,
        company_id=company_id,
    )


# ============================================================================
# Core Execution Logic
# ============================================================================
async def execute_action(
    event: Message,
    user: User,
    state: FSMContext,
    req_id: int,
    action: str,
    reason: Optional[str],
    is_callback: bool,
    company_id: Optional[int] = None,
):
    """Execute the selected action via the Service layer."""
    if not company_id:
        msg = "⛔ خطأ: لا يوجد سياق شركة."
        if is_callback:
            await event.edit_text(msg, parse_mode="HTML")
        else:
            await event.answer(msg, parse_mode="HTML")
        await state.clear()
        return

    success = False
    action_text = ""

    if action == "submit":
        success = await purchase_request_service.submit_request(req_id, company_id)
        action_text = "إرسال للمراجعة"
    elif action == "review":
        success = await purchase_request_service.start_review(req_id, company_id, user.id)
        action_text = "بدء المراجعة"
    elif action == "approve":
        success = await purchase_request_service.approve_request(req_id, company_id, user.id)
        action_text = "الاعتماد"
    elif action == "reject":
        success = await purchase_request_service.reject_request(req_id, company_id, user.id, reason)
        action_text = "الرفض"
    elif action == "revision":
        success = await purchase_request_service.request_revision(req_id, company_id, user.id, reason)
        action_text = "طلب التعديل"
    elif action == "cancel":
        success = await purchase_request_service.cancel_request(req_id, company_id, user.id)
        action_text = "الإلغاء"

    if success:
        msg = f"✅ تم تنفيذ إجراء <b>{action_text}</b> على الطلب <code>{req_id}</code> بنجاح."
        if reason:
            msg += f"\n📝 السبب: {reason}"

        if is_callback:
            await event.edit_text(msg, parse_mode="HTML")
        else:
            await event.answer(msg, parse_mode="HTML")

        await notify_requester(
            event.bot, req_id, company_id, action_text, reason, user.full_name
        )
    else:
        msg = f"❌ فشل تنفيذ إجراء {action_text}. تأكد من صلاحيتك وحالة الطلب."
        if is_callback:
            await event.edit_text(msg, parse_mode="HTML")
        else:
            await event.answer(msg, parse_mode="HTML")

    await state.clear()


# ============================================================================
# Notification Helper
# ============================================================================
async def notify_requester(
    bot,
    req_id: int,
    company_id: int,
    action_text: str,
    reason: Optional[str],
    changer_name: str,
):
    """Notify the original requester about the manual intervention."""
    try:
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(PurchaseRequest, User.telegram_user_id)
                .join(User, PurchaseRequest.requester_id == User.id)
                .where(PurchaseRequest.id == req_id)
            )
            result = await session.execute(stmt)
            row = result.one_or_none()

            if row:
                req, tg_id = row
                text = (
                    f"🔄 <b>تم تحديث حالة طلبك</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"🔢 رقم الطلب: <code>{req_id}</code>\n"
                    f"📝 العنوان: {req.title}\n"
                    f"⚡ الإجراء: {action_text}\n"
                    f"👤 بواسطة: {changer_name}\n"
                )
                if reason:
                    text += f"💬 السبب/الملاحظات: {reason}\n"

                await bot.send_message(tg_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to notify requester | request_id=%s", req_id)


# ============================================================================
# Cancel Handlers
# ============================================================================
@router.callback_query(F.data == "cancel_manage_flow")
async def callback_cancel_flow(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("✅ تم إلغاء العملية.")
    await callback.answer()


@router.message(
    StateFilter(
        ManageRequestFlow.waiting_for_request_id,
        ManageRequestFlow.waiting_for_action,
        ManageRequestFlow.waiting_for_reason,
    ),
    F.text == "إلغاء",
)
async def cancel_by_text(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ تم إلغاء العملية.")
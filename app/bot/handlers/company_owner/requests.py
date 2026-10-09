"""
Company Owner - Purchase Request handlers.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.
"""
from typing import List, Optional
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.core.logger import get_logger
from app.database.database import get_session
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.user import User

from app.bot.handlers.company_owner._helpers import (
    DEFAULT_SKIP_REASON,
    require_company_owner_or_manager,
    require_buyer_admin_access,
    _execute_pr_decision,
)

logger = get_logger(__name__)
router = Router(name="company_owner_requests")


# ============================================================================
# FSM States
# ============================================================================
class PRDecisionFlow(StatesGroup):
    waiting_for_rejection_reason = State()
    waiting_for_revision_reason = State()


# ============================================================================
# Button: "📋 طلبات الشراء"
# ============================================================================
@router.message(F.text == "📋 طلبات الشراء")
async def btn_list_purchase_requests(
    message: Message,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle '📋 طلبات الشراء' button."""
    if not require_buyer_admin_access(roles):
        await message.answer("⛔ غير مصرح لك بالوصول لهذه الوظيفة.")
        return

    if not user or not company_id:
        await message.answer("⚠️ لم يتم العثور على شركتك. يرجى التواصل مع الدعم.")
        return

    logger.info(
        "COMPANY_OWNER | viewing purchase requests | tg_id=%s | company_id=%s",
        message.from_user.id,
        company_id,
    )

    try:
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(PurchaseRequest)
                .where(PurchaseRequest.company_id == company_id)
                .order_by(PurchaseRequest.created_at.desc())
                .limit(10)
            )
            result = await session.execute(stmt)
            requests = result.scalars().all()

        if not requests:
            await message.answer(
                "📭 <b>لا توجد طلبات شراء حالياً.</b>\n\n"
                "يمكنك إنشاء طلب شراء جديد من القائمة الرئيسية.",
                parse_mode="HTML"
            )
            return

        await message.answer(
            f"📋 <b>طلبات الشراء الأخيرة ({len(requests)} طلب):</b>\n"
            f"━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML"
        )

        for i, req in enumerate(requests, 1):
            status = req.status.value if hasattr(req.status, 'value') else str(req.status)
            date_str = req.created_at.strftime("%Y-%m-%d %H:%M") if req.created_at else "N/A"

            card_text = (
                f"\n<b>{i}. 📋 {req.title}</b>\n"
                f"   📊 الحالة: <b>{status}</b>\n"
                f"   📅 التاريخ: {date_str}\n"
                f"   🆔 رقم الطلب: <code>{req.id}</code>"
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="👁️ عرض التفاصيل",
                    callback_data=f"co_view_pr:{req.id}"
                )]
            ])

            await message.answer(card_text, reply_markup=keyboard, parse_mode="HTML")

        if len(requests) == 10:
            await message.answer("<i>📌 يتم عرض آخر 10 طلبات فقط.</i>", parse_mode="HTML")

    except Exception:
        logger.exception("Error fetching purchase requests")
        await message.answer("❌ عذراً، حدث خطأ أثناء جلب الطلبات.")


# ============================================================================
# Callback: View Purchase Request Details
# ============================================================================
@router.callback_query(F.data.startswith("co_view_pr:"))
async def callback_view_purchase_request(
    callback: CallbackQuery,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle view purchase request details with decision buttons."""
    if not require_buyer_admin_access(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])

    try:
        async with get_session(company_id=company_id) as session:
            stmt = select(PurchaseRequest).where(
                PurchaseRequest.id == request_id,
                PurchaseRequest.company_id == company_id
            )
            result = await session.execute(stmt)
            request = result.scalar_one_or_none()

        if not request:
            await callback.answer("❌ لم يتم العثور على الطلب.", show_alert=True)
            return

        status = request.status.value if hasattr(request.status, 'value') else str(request.status)

        requester_name = f"ID {request.requester_id}"
        try:
            async with get_session(company_id=company_id) as session:
                stmt_user = select(User).where(User.id == request.requester_id)
                result_user = await session.execute(stmt_user)
                requester = result_user.scalar_one_or_none()
                if requester:
                    requester_name = requester.full_name
        except Exception:
            logger.exception("Could not fetch requester name | request_id=%s", request.id)

        details = (
            f"📋 <b>تفاصيل طلب الشراء #{request.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 العنوان: <b>{request.title}</b>\n"
            f"📊 الحالة: <b>{status}</b>\n"
            f"📅 تاريخ الإنشاء: {request.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"👤 مقدم الطلب: {requester_name}\n\n"
        )

        if request.decision_note:
            details += f"📝 ملاحظات القرار: {request.decision_note}\n\n"

        details += "<i>💡 تفاصيل البنود والمواصفات ستتوفر قريباً.</i>"

        keyboard_buttons = []
        reviewable_states = ["submitted", "under_review"]

        if status in reviewable_states and require_company_owner_or_manager(roles):
            keyboard_buttons = [
                [
                    InlineKeyboardButton(text="✅ اعتماد الطلب", callback_data=f"co_approve_pr:{request.id}"),
                    InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"co_reject_pr_start:{request.id}")
                ],
                [
                    InlineKeyboardButton(text="✏️ طلب تعديل", callback_data=f"co_request_revision_start:{request.id}")
                ]
            ]

        if keyboard_buttons:
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            await callback.message.answer(details, reply_markup=keyboard, parse_mode="HTML")
        else:
            await callback.message.answer(details, parse_mode="HTML")

        await callback.answer()

    except Exception:
        logger.exception("Error viewing purchase request")
        await callback.answer("❌ حدث خطأ.", show_alert=True)


# ============================================================================
# Callback: Approve Purchase Request
# ============================================================================
@router.callback_query(F.data.startswith("co_approve_pr:"))
async def callback_approve_pr(
    callback: CallbackQuery,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle approve purchase request button."""
    if not require_company_owner_or_manager(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])

    try:
        notification_template = (
            "✅ <b>تم اعتماد طلب الشراء الخاص بك!</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "📋 الطلب: <b>{title}</b>\n"
            "🆔 رقم الطلب: <code>{request_id}</code>\n"
            "👤 اعتمده: {decided_by}\n\n"
            "🎉 سيتم إنشاء طلب عروض الأسعار (RFQ) قريباً."
        )

        success, result = await _execute_pr_decision(
            message_or_callback=callback,
            bot=callback.bot,
            user=user,
            request_id=request_id,
            company_id=company_id,
            new_status_str="approved",
            reason="تم الاعتماد",
            decision_label="اعتماد",
            decision_emoji="✅",
            notification_template=notification_template,
        )

        if not success:
            await callback.answer(f"❌ {result}", show_alert=True)
            return

        request = result

        await callback.message.edit_text(
            f"✅ <b>تم اعتماد طلب الشراء #{request.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 العنوان: <b>{request.title}</b>\n"
            f"📊 الحالة الجديدة: <b>approved</b>\n"
            f"👤 اعتمده: {user.full_name}\n"
            f"📅 وقت الاعتماد: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📨 تم إشعار مقدم الطلب بالموافقة.",
            parse_mode="HTML"
        )

        await callback.answer("✅ تم اعتماد الطلب بنجاح!", show_alert=False)

    except Exception:
        logger.exception("Error approving purchase request")
        await callback.answer("❌ حدث خطأ أثناء الاعتماد.", show_alert=True)


# ============================================================================
# Callback: Start Reject Purchase Request (FSM)
# ============================================================================
@router.callback_query(F.data.startswith("co_reject_pr_start:"))
async def callback_start_reject_pr(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Start the rejection process - ask for reason with skip option."""
    if not require_company_owner_or_manager(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])
    await state.update_data(rejecting_pr_id=request_id, company_id=company_id)

    skip_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏭️ تخطي السبب",
            callback_data=f"co_skip_reject_reason:{request_id}"
        )]
    ])

    await callback.message.answer(
        "❌ <b>رفض طلب الشراء</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 الرجاء إدخال سبب الرفض:\n"
        "(مثال: المواصفات غير واضحة، الكمية غير كافية)\n\n"
        "أو اضغط '⏭️ تخطي السبب' للرفض بدون سبب.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )
    await state.set_state(PRDecisionFlow.waiting_for_rejection_reason)
    await callback.answer()


# ============================================================================
# Callback: Skip Rejection Reason
# ============================================================================
@router.callback_query(F.data.startswith("co_skip_reject_reason:"))
async def callback_skip_reject_reason(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle skip rejection reason button."""
    if not require_company_owner_or_manager(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])

    try:
        notification_template = (
            "❌ <b>تم رفض طلب الشراء الخاص بك</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "📋 الطلب: <b>{title}</b>\n"
            "🆔 رقم الطلب: <code>{request_id}</code>\n"
            "👤 رفضه: {decided_by}\n\n"
            "📝 <b>سبب الرفض:</b>\n{reason}\n\n"
            "💡 يمكنك إنشاء طلب شراء جديد بعد معالجة الملاحظات."
        )

        success, result = await _execute_pr_decision(
            message_or_callback=callback,
            bot=callback.bot,
            user=user,
            request_id=request_id,
            company_id=company_id,
            new_status_str="rejected",
            reason=DEFAULT_SKIP_REASON,
            decision_label="رفض",
            decision_emoji="❌",
            notification_template=notification_template,
        )

        if not success:
            await callback.answer(f"❌ {result}", show_alert=True)
            return

        request = result

        await callback.message.edit_text(
            f"❌ <b>تم رفض طلب الشراء #{request.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 العنوان: <b>{request.title}</b>\n"
            f"📊 الحالة الجديدة: <b>rejected</b>\n"
            f"👤 رفضه: {user.full_name}\n"
            f"📅 وقت الرفض: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📝 السبب: {DEFAULT_SKIP_REASON}\n"
            f"📨 تم إشعار مقدم الطلب بالرفض.",
            parse_mode="HTML"
        )

        await state.clear()
        await callback.answer("❌ تم رفض الطلب بنجاح!", show_alert=False)

    except Exception:
        logger.exception("Error rejecting purchase request (skip)")
        await callback.answer("❌ حدث خطأ أثناء الرفض.", show_alert=True)
        await state.clear()


# ============================================================================
# Callback: Start Request Revision (FSM)
# ============================================================================
@router.callback_query(F.data.startswith("co_request_revision_start:"))
async def callback_start_request_revision(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Start the revision request process - ask for reason with skip option."""
    if not require_company_owner_or_manager(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])
    await state.update_data(revision_pr_id=request_id, company_id=company_id)

    skip_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏭️ تخطي السبب",
            callback_data=f"co_skip_revision_reason:{request_id}"
        )]
    ])

    await callback.message.answer(
        "✏️ <b>طلب تعديل على طلب الشراء</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 الرجاء إدخال التعديلات المطلوبة:\n"
        "(مثال: يرجى توضيح المواصفات، إضافة تاريخ التسليم)\n\n"
        "أو اضغط '⏭️ تخطي السبب' بدون تحديد تعديلات.",
        reply_markup=skip_keyboard,
        parse_mode="HTML"
    )
    await state.set_state(PRDecisionFlow.waiting_for_revision_reason)
    await callback.answer()


# ============================================================================
# Callback: Skip Revision Reason
# ============================================================================
@router.callback_query(F.data.startswith("co_skip_revision_reason:"))
async def callback_skip_revision_reason(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle skip revision reason button."""
    if not require_company_owner_or_manager(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])

    try:
        notification_template = (
            "✏️ <b>طلب تعديل على طلب الشراء الخاص بك</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "📋 الطلب: <b>{title}</b>\n"
            "🆔 رقم الطلب: <code>{request_id}</code>\n"
            "👤 طلب التعديل: {decided_by}\n\n"
            "📝 <b>التعديلات المطلوبة:</b>\n{reason}\n\n"
            "💡 يرجى تعديل الطلب وإعادة إرساله للمراجعة."
        )

        success, result = await _execute_pr_decision(
            message_or_callback=callback,
            bot=callback.bot,
            user=user,
            request_id=request_id,
            company_id=company_id,
            new_status_str="needs_revision",
            reason=DEFAULT_SKIP_REASON,
            decision_label="طلب تعديل",
            decision_emoji="✏️",
            notification_template=notification_template,
        )

        if not success:
            await callback.answer(f"❌ {result}", show_alert=True)
            return

        request = result

        await callback.message.edit_text(
            f"✏️ <b>تم طلب تعديل على طلب الشراء #{request.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 العنوان: <b>{request.title}</b>\n"
            f"📊 الحالة الجديدة: <b>needs_revision</b>\n"
            f"👤 طلب التعديل: {user.full_name}\n"
            f"📅 وقت الطلب: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📝 التعديلات: {DEFAULT_SKIP_REASON}\n"
            f"📨 تم إشعار مقدم الطلب بطلب التعديل.",
            parse_mode="HTML"
        )

        await state.clear()
        await callback.answer("✏️ تم طلب التعديل بنجاح!", show_alert=False)

    except Exception:
        logger.exception("Error requesting revision (skip)")
        await callback.answer("❌ حدث خطأ أثناء طلب التعديل.", show_alert=True)
        await state.clear()


# ============================================================================
# FSM: Process Rejection Reason (typed by user)
# ============================================================================
@router.message(PRDecisionFlow.waiting_for_rejection_reason)
async def process_rejection_reason(
    message: Message,
    state: FSMContext,
    user,
    roles: List[str]
):
    """Process purchase request rejection reason input."""
    if not require_company_owner_or_manager(roles):
        await message.answer("⛔ غير مصرح لك.")
        await state.clear()
        return

    reason = message.text.strip()
    data = await state.get_data()
    request_id = data.get("rejecting_pr_id")
    company_id = data.get("company_id")

    if not request_id or not reason:
        await message.answer("❌ حدث خطأ. الرجاء البدء من جديد.")
        await state.clear()
        return

    try:
        notification_template = (
            "❌ <b>تم رفض طلب الشراء الخاص بك</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "📋 الطلب: <b>{title}</b>\n"
            "🆔 رقم الطلب: <code>{request_id}</code>\n"
            "👤 رفضه: {decided_by}\n\n"
            "📝 <b>سبب الرفض:</b>\n{reason}\n\n"
            "💡 يمكنك إنشاء طلب شراء جديد بعد معالجة الملاحظات."
        )

        success, result = await _execute_pr_decision(
            message_or_callback=message,
            bot=message.bot,
            user=user,
            request_id=request_id,
            company_id=company_id,
            new_status_str="rejected",
            reason=reason,
            decision_label="رفض",
            decision_emoji="❌",
            notification_template=notification_template,
        )

        if not success:
            await message.answer(f"❌ {result}")
            await state.clear()
            return

        request = result

        await message.answer(
            f"❌ <b>تم رفض طلب الشراء #{request.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 العنوان: <b>{request.title}</b>\n"
            f"📊 الحالة الجديدة: <b>rejected</b>\n"
            f"👤 رفضه: {user.full_name}\n"
            f"📅 وقت الرفض: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📝 السبب: {reason}\n"
            f"📨 تم إشعار مقدم الطلب بالرفض.",
            parse_mode="HTML"
        )

        await state.clear()

    except Exception:
        logger.exception("Error rejecting purchase request")
        await message.answer("❌ حدث خطأ أثناء الرفض.")
        await state.clear()


# ============================================================================
# FSM: Process Revision Reason (typed by user)
# ============================================================================
@router.message(PRDecisionFlow.waiting_for_revision_reason)
async def process_revision_reason(
    message: Message,
    state: FSMContext,
    user,
    roles: List[str]
):
    """Process purchase request revision reason input."""
    if not require_company_owner_or_manager(roles):
        await message.answer("⛔ غير مصرح لك.")
        await state.clear()
        return

    reason = message.text.strip()
    data = await state.get_data()
    request_id = data.get("revision_pr_id")
    company_id = data.get("company_id")

    if not request_id or not reason:
        await message.answer("❌ حدث خطأ. الرجاء البدء من جديد.")
        await state.clear()
        return

    try:
        notification_template = (
            "✏️ <b>طلب تعديل على طلب الشراء الخاص بك</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "📋 الطلب: <b>{title}</b>\n"
            "🆔 رقم الطلب: <code>{request_id}</code>\n"
            "👤 طلب التعديل: {decided_by}\n\n"
            "📝 <b>التعديلات المطلوبة:</b>\n{reason}\n\n"
            "💡 يرجى تعديل الطلب وإعادة إرساله للمراجعة."
        )

        success, result = await _execute_pr_decision(
            message_or_callback=message,
            bot=message.bot,
            user=user,
            request_id=request_id,
            company_id=company_id,
            new_status_str="needs_revision",
            reason=reason,
            decision_label="طلب تعديل",
            decision_emoji="✏️",
            notification_template=notification_template,
        )

        if not success:
            await message.answer(f"❌ {result}")
            await state.clear()
            return

        request = result

        await message.answer(
            f"✏️ <b>تم طلب تعديل على طلب الشراء #{request.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 العنوان: <b>{request.title}</b>\n"
            f"📊 الحالة الجديدة: <b>needs_revision</b>\n"
            f"👤 طلب التعديل: {user.full_name}\n"
            f"📅 وقت الطلب: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📝 التعديلات المطلوبة: {reason}\n"
            f"📨 تم إشعار مقدم الطلب بطلب التعديل.",
            parse_mode="HTML"
        )

        await state.clear()

    except Exception:
        logger.exception("Error requesting revision")
        await message.answer("❌ حدث خطأ أثناء طلب التعديل.")
        await state.clear()
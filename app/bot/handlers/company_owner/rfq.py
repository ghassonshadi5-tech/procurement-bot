"""
Company Owner - RFQ handlers.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.
"""
from typing import List, Optional
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.core.logger import get_logger
from app.database.database import get_session
from app.database.models.rfq import Rfq

from app.bot.handlers.company_owner._helpers import require_buyer_admin_access

logger = get_logger(__name__)
router = Router(name="company_owner_rfq")


# ============================================================================
# Button: "📨 طلبات عروض الأسعار (RFQ)"
# ============================================================================
@router.message(F.text == "📨 طلبات عروض الأسعار (RFQ)")
async def btn_list_rfqs(
    message: Message,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle '📨 طلبات عروض الأسعار (RFQ)' button."""
    if not require_buyer_admin_access(roles):
        await message.answer("⛔ غير مصرح لك بالوصول لهذه الوظيفة.")
        return

    if not user or not company_id:
        await message.answer("⚠️ لم يتم العثور على شركتك.")
        return

    logger.info(
        "COMPANY_OWNER | viewing RFQs | tg_id=%s | company_id=%s",
        message.from_user.id,
        company_id,
    )

    try:
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(Rfq)
                .where(Rfq.company_id == company_id)
                .order_by(Rfq.created_at.desc())
                .limit(10)
            )
            result = await session.execute(stmt)
            rfqs = result.scalars().all()

        if not rfqs:
            await message.answer(
                "📭 <b>لا توجد طلبات عروض أسعار (RFQ) حالياً.</b>\n\n"
                "يتم إنشاء RFQ تلقائياً بعد اعتماد طلب الشراء.",
                parse_mode="HTML"
            )
            return

        await message.answer(
            f"📨 <b>طلبات عروض الأسعار (RFQ) الأخيرة ({len(rfqs)} طلب):</b>\n"
            f"━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML"
        )

        for i, rfq in enumerate(rfqs, 1):
            status = rfq.status.value if hasattr(rfq.status, 'value') else str(rfq.status)
            method = rfq.method.value if hasattr(rfq.method, 'value') and rfq.method else "غير محدد"
            date_str = rfq.created_at.strftime("%Y-%m-%d %H:%M") if rfq.created_at else "N/A"
            deadline_str = rfq.deadline.strftime("%Y-%m-%d %H:%M") if rfq.deadline else "غير محدد"

            card_text = (
                f"\n<b>{i}. 📨 RFQ #{rfq.id}</b>\n"
                f"   🎯 الأسلوب: <b>{method}</b>\n"
                f"   📊 الحالة: <b>{status}</b>\n"
                f"   ⏰ الموعد النهائي: {deadline_str}\n"
                f"   📅 الإنشاء: {date_str}"
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="👁️ عرض التفاصيل والعروض",
                    callback_data=f"co_view_rfq:{rfq.id}"
                )]
            ])

            await message.answer(card_text, reply_markup=keyboard, parse_mode="HTML")

        if len(rfqs) == 10:
            await message.answer("<i>📌 يتم عرض آخر 10 طلبات فقط.</i>", parse_mode="HTML")

    except Exception:
        logger.exception("Error fetching RFQs")
        await message.answer("❌ عذراً، حدث خطأ أثناء جلب طلبات العروض.")


# ============================================================================
# Callback: View RFQ Details
# ============================================================================
@router.callback_query(F.data.startswith("co_view_rfq:"))
async def callback_view_rfq(
    callback: CallbackQuery,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle view RFQ details."""
    if not require_buyer_admin_access(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    rfq_id = int(callback.data.split(":", 1)[1])

    try:
        async with get_session(company_id=company_id) as session:
            stmt = select(Rfq).where(
                Rfq.id == rfq_id,
                Rfq.company_id == company_id
            )
            result = await session.execute(stmt)
            rfq = result.scalar_one_or_none()

        if not rfq:
            await callback.answer("❌ لم يتم العثور على طلب العرض.", show_alert=True)
            return

        status = rfq.status.value if hasattr(rfq.status, 'value') else str(rfq.status)
        method = rfq.method.value if hasattr(rfq.method, 'value') and rfq.method else "غير محدد"

        details = (
            f"📨 <b>تفاصيل طلب العرض #{rfq.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 الحالة: <b>{status}</b>\n"
            f"🎯 الأسلوب: <b>{method}</b>\n"
            f"⏰ الموعد النهائي: {rfq.deadline.strftime('%Y-%m-%d %H:%M') if rfq.deadline else 'غير محدد'}\n"
            f"📅 تاريخ الإنشاء: {rfq.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"<i>💡 عرض المقارنة والعروض المقدمة سيتوفر قريباً.</i>"
        )

        await callback.message.answer(details, parse_mode="HTML")
        await callback.answer()

    except Exception:
        logger.exception("Error viewing RFQ")
        await callback.answer("❌ حدث خطأ.", show_alert=True)
"""
Company Owner - Suppliers management handlers.
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
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, CopyTextButton,
)
from sqlalchemy import select, or_

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session

from app.bot.handlers.company_owner._helpers import (
    normalize_phone_number,
    require_company_owner_or_manager,
    require_buyer_admin_access,
)

logger = get_logger(__name__)
router = Router(name="company_owner_suppliers")


# ============================================================================
# FSM States
# ============================================================================
class SupplierInviteFlow(StatesGroup):
    waiting_for_search_query = State()
    waiting_for_contact = State()


# ============================================================================
# Button: "🏪 إدارة الموردين"
# ============================================================================
@router.message(F.text == "🏪 إدارة الموردين")
async def btn_manage_suppliers(
    message: Message,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle '🏪 إدارة الموردين' button."""
    if not require_buyer_admin_access(roles):
        await message.answer("⛔ غير مصرح لك بالوصول لهذه الوظيفة.")
        return

    if not user or not company_id:
        await message.answer("⚠️ لم يتم العثور على شركتك.")
        return

    logger.info(
        "COMPANY_OWNER | managing suppliers | tg_id=%s | company_id=%s",
        message.from_user.id,
        company_id,
    )

    try:
        from app.database.models.supplier_relation import SupplierRelation
        from app.database.models.supplier import Supplier

        async with get_session(company_id=company_id) as session:
            stmt = (
                select(SupplierRelation, Supplier)
                .join(Supplier, SupplierRelation.supplier_id == Supplier.id)
                .where(SupplierRelation.company_id == company_id)
                .order_by(SupplierRelation.id.desc())
            )
            result = await session.execute(stmt)
            relations = result.all()

        if not relations:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="➕ دعوة مورد جديد",
                    callback_data="co_invite_supplier"
                )]
            ])
            await message.answer(
                "🏪 <b>لا يوجد موردون مرتبطون بشركتك حالياً.</b>\n\n"
                "يمكنك دعوة موردين جدد للانضمام إلى شبكتك.",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            return

        response = (
            f"🏪 <b>الموردون المرتبطون بشركتك ({len(relations)} مورد):</b>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )

        for i, (rel, supplier) in enumerate(relations[:15], 1):
            status = rel.status.value if hasattr(rel.status, 'value') else str(rel.status)
            cities = ', '.join(supplier.cities) if supplier.cities else 'غير محدد'

            response += (
                f"\n\n<b>{i}. 🏭 {supplier.name}</b>\n"
                f"   🏙️ المدن: {cities}\n"
                f"   🏷️ النشاط: {supplier.activity or 'غير محدد'}\n"
                f"   📊 الحالة: <b>{status}</b>\n"
                f"   🆔 رقم المورد: <code>{supplier.id}</code>"
            )

        if len(relations) > 15:
            response += f"\n\n<i>... و {len(relations) - 15} موردون آخرون</i>"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="➕ دعوة مورد جديد",
                callback_data="co_invite_supplier"
            )]
        ])

        await message.answer(response, reply_markup=keyboard, parse_mode="HTML")

    except Exception:
        logger.exception("Error fetching supplier relations")
        await message.answer("❌ عذراً، حدث خطأ أثناء جلب الموردين.")


# ============================================================================
# Callback: Invite Supplier - Show Options Menu
# ============================================================================
@router.callback_query(F.data == "co_invite_supplier")
async def callback_invite_supplier(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle invite supplier button - show invitation options."""
    if not require_company_owner_or_manager(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    if not permissions.is_company_owner(roles):
        await callback.answer("⛔ هذه الوظيفة متاحة لمالك الشركة فقط.", show_alert=True)
        return

    options_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔍 البحث عن مورد مسجل في النظام",
            callback_data="co_invite_search"
        )],
        [InlineKeyboardButton(
            text="🔗 إنشاء رابط دعوة لمورد جديد",
            callback_data="co_invite_generate_link"
        )],
        [InlineKeyboardButton(
            text="❌ إلغاء",
            callback_data="co_invite_cancel"
        )]
    ])

    await callback.message.answer(
        "🏪 <b>دعوة مورد جديد</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "اختر طريقة دعوة المورد:\n\n"
        "🔍 <b>البحث عن مورد مسجل:</b>\n"
        "ابحث بالاسم أو الرقم الضريبي (ИНН) عن مورد موجود في النظام.\n\n"
        "🔗 <b>إنشاء رابط دعوة:</b>\n"
        "أنشئ رابطاً فريداً لمرة واحدة وأرسله للمورد للتسجيل والربط بشركتك.",
        reply_markup=options_keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================================
# Callback: Generate Supplier Invitation Link
# ============================================================================
@router.callback_query(F.data == "co_invite_generate_link")
async def callback_generate_supplier_link(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Generate a one-time supplier invitation deep link with share button."""
    if not permissions.is_company_owner(roles):
        await callback.answer("⛔ هذه الوظيفة متاحة لمالك الشركة فقط.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    try:
        from app.database.models.company_invitation import CompanyInvitation
        from app.database.models.role import Role
        from app.database.models.company import Company
        from app.config.settings import settings

        token = secrets.token_hex(24)

        async with get_session(company_id=company_id) as session:
            stmt = select(Role).where(Role.code == "supplier_owner")
            result = await session.execute(stmt)
            role = result.scalar_one_or_none()

            if not role:
                await callback.answer("❌ خطأ: لم يتم العثور على دور المورد.", show_alert=True)
                return

            invitation = CompanyInvitation(
                company_id=company_id,
                role_id=role.id,
                token=token,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=48)
            )
            session.add(invitation)

            comp_stmt = select(Company).where(Company.id == company_id)
            comp_res = await session.execute(comp_stmt)
            company_obj = comp_res.scalar_one_or_none()
            company_name = company_obj.name if company_obj else "الشركة"

            await session.commit()

        bot_username = settings.BOT_USERNAME
        invite_link = f"https://t.me/{bot_username}?start=sup_{token}"

        action_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 نسخ الرابط",
                    copy_text=CopyTextButton(text=invite_link)
                ),
                InlineKeyboardButton(
                    text="📤 مشاركة الرابط مع...",
                    url=f"https://t.me/share/url?url={invite_link}&text=مرحباً، أدعوك للانضمام كمورد في {company_name}"
                )
            ],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="co_invite_cancel")]
        ])

        await callback.message.answer(
            f"🔗 <b>رابط دعوة مورد جديد</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏢 الشركة: <b>{company_name}</b>\n"
            f"👤 الدور: <b>مالك المورد (Supplier Owner)</b>\n"
            f"⏰ الصلاحية: <b>48 ساعة</b>\n"
            f"🔒 الاستخدام: <b>مرة واحدة فقط</b>\n\n"
            f"📋 <b>كيفية إرسال الدعوة:</b>\n"
            f"1️⃣ اضغط '📋 نسخ الرابط' لنسخه يدوياً\n"
            f"2️⃣ أو اضغط '📤 مشاركة الرابط' لاختيار شخص من جهات اتصالك مباشرة\n\n"
            f"🔗 الرابط:\n<code>{invite_link}</code>",
            reply_markup=action_keyboard,
            parse_mode="HTML"
        )

        logger.info(
            "SUPPLIER_INVITE | generated invite link | company_id=%s | user_id=%s | token_prefix=%s",
            company_id,
            user.id,
            token[:8],
        )

        await callback.answer("✅ تم إنشاء رابط الدعوة بنجاح!", show_alert=False)

    except Exception:
        logger.exception("Error generating supplier invite link")
        await callback.answer("❌ حدث خطأ أثناء إنشاء رابط الدعوة.", show_alert=True)


# ============================================================================
# Callback: Start Search Flow
# ============================================================================
@router.callback_query(F.data == "co_invite_search")
async def callback_invite_search(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Start supplier search flow."""
    if not permissions.is_company_owner(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    await state.update_data(company_id=company_id)
    await state.set_state(SupplierInviteFlow.waiting_for_search_query)

    await callback.message.answer(
        "🔍 <b>البحث عن مورد</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 أدخل اسم المورد أو الرقم الضريبي (ИНН) للبحث:\n\n"
        "<i>مثال: شركة الأمل للتوريدات أو 7701234567</i>",
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================================
# FSM: Process Search Query
# ============================================================================
@router.message(SupplierInviteFlow.waiting_for_search_query)
async def process_supplier_search(
    message: Message,
    state: FSMContext,
    user,
    roles: List[str]
):
    """Process supplier search query."""
    if not permissions.is_company_owner(roles):
        await message.answer("⛔ غير مصرح لك.")
        await state.clear()
        return

    query = message.text.strip()
    data = await state.get_data()
    company_id = data.get("company_id")

    if not query or len(query) < 2:
        await message.answer("❌ الرجاء إدخال اسم أو رقم ضريبي صحيح (حرفان على الأقل).")
        return

    try:
        from app.database.models.supplier import Supplier
        from app.database.models.supplier_relation import SupplierRelation

        async with get_session(company_id=company_id) as session:
            stmt = (
                select(Supplier)
                .where(
                    Supplier.status == "active",
                    or_(
                        Supplier.name.ilike(f"%{query}%"),
                        Supplier.inn.ilike(f"%{query}%")
                    )
                )
                .order_by(Supplier.id.desc())
                .limit(10)
            )
            result = await session.execute(stmt)
            suppliers = result.scalars().all()

        if not suppliers:
            await message.answer(
                "📭 <b>لم يتم العثور على موردين.</b>\n\n"
                "لم نعثر على مورد مطابق لبحثك.\n"
                "جرب البحث باسم مختلف أو تأكد من الرقم الضريبي.\n\n"
                "💡 إذا كان المورد غير مسجل في النظام، يمكنك دعوته عبر جهة الاتصال.",
                parse_mode="HTML"
            )
            await state.clear()
            return

        supplier_ids = [s.id for s in suppliers]
        async with get_session(company_id=company_id) as session:
            stmt = select(SupplierRelation.supplier_id).where(
                SupplierRelation.company_id == company_id,
                SupplierRelation.supplier_id.in_(supplier_ids)
            )
            result = await session.execute(stmt)
            existing_relations = {row[0] for row in result.all()}

        keyboard_buttons = []
        response_text = (
            f"🔍 <b>نتائج البحث ({len(suppliers)} مورد):</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
        )

        available_count = 0
        for i, supplier in enumerate(suppliers, 1):
            cities = ', '.join(supplier.cities) if supplier.cities else 'غير محدد'

            if supplier.id in existing_relations:
                response_text += (
                    f"\n<b>{i}. 🏭 {supplier.name}</b>\n"
                    f"   🏙️ المدن: {cities}\n"
                    f"   ⚠️ <i>مرتبط بشركتك بالفعل</i>\n"
                )
            else:
                available_count += 1
                response_text += (
                    f"\n<b>{i}. 🏭 {supplier.name}</b>\n"
                    f"   🏙️ المدن: {cities}\n"
                    f"   🏷️ النشاط: {supplier.activity or 'غير محدد'}\n"
                )
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text=f"🏭 {supplier.name}",
                        callback_data=f"co_invite_select:{supplier.id}"
                    )
                ])

        if available_count == 0:
            response_text += "\n\n⚠️ جميع الموردين في النتائج مرتبطون بشركتك بالفعل."
            await message.answer(response_text, parse_mode="HTML")
            await state.clear()
            return

        response_text += "\n\n👇 اختر المورد الذي تريد دعوته:"
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        await message.answer(response_text, reply_markup=keyboard, parse_mode="HTML")

    except Exception:
        logger.exception("Error searching suppliers")
        await message.answer("❌ عذراً، حدث خطأ أثناء البحث.")
        await state.clear()


# ============================================================================
# Callback: Select Supplier from Search Results
# ============================================================================
@router.callback_query(F.data.startswith("co_invite_select:"))
async def callback_select_supplier(
    callback: CallbackQuery,
    state: FSMContext,
    user,
    roles: List[str],
    company_id: Optional[int]
):
    """Handle supplier selection from search results."""
    if not permissions.is_company_owner(roles):
        await callback.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    if not user or not company_id:
        await callback.answer("⚠️ لم يتم العثور على شركتك.", show_alert=True)
        return

    supplier_id = int(callback.data.split(":", 1)[1])

    try:
        success, message_text = await _create_supplier_relation(
            company_id=company_id,
            supplier_id=supplier_id,
            invited_by=user,
            bot=callback.bot,
        )

        if success:
            await callback.message.answer(message_text, parse_mode="HTML")
            await callback.answer("✅ تم إرسال طلب الربط بنجاح!", show_alert=False)
        else:
            await callback.answer(f"❌ {message_text}", show_alert=True)

        await state.clear()

    except Exception:
        logger.exception("Error selecting supplier")
        await callback.answer("❌ حدث خطأ.", show_alert=True)
        await state.clear()


# ============================================================================
# Callback: Cancel Invitation
# ============================================================================
@router.callback_query(F.data == "co_invite_cancel")
async def callback_cancel_invitation(
    callback: CallbackQuery,
    state: FSMContext
):
    """Cancel the invitation process."""
    await callback.message.answer("❌ تم إلغاء عملية الدعوة.")
    await state.clear()
    await callback.answer()


# ============================================================================
# Helper: Create Supplier Relation (Core Logic)
# ============================================================================
async def _create_supplier_relation(
    company_id: int,
    supplier_id: int,
    invited_by,
    bot
) -> tuple:
    """
    Create a supplier relation (link request) between company and supplier.
    Returns (success: bool, message: str).
    """
    from app.database.models.supplier_relation import SupplierRelation
    from app.database.models.supplier import Supplier
    from app.database.models.user import User

    async with get_session(company_id=company_id) as session:
        stmt = select(SupplierRelation).where(
            SupplierRelation.company_id == company_id,
            SupplierRelation.supplier_id == supplier_id
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            status = existing.status.value if hasattr(existing.status, 'value') else str(existing.status)
            if status == "active":
                return False, "هذا المورد مرتبط بشركتك بالفعل."
            elif status == "pending":
                return False, "يوجد طلب ربط معلق بالفعل مع هذا المورد."
            elif status == "suspended":
                return False, "العلاقة مع هذا المورد موقوفة حالياً. تواصل مع الدعم لإعادة تفعيلها."

        stmt_supplier = select(Supplier).where(Supplier.id == supplier_id)
        result_supplier = await session.execute(stmt_supplier)
        supplier = result_supplier.scalar_one_or_none()

        if not supplier:
            return False, "لم يتم العثور على المورد."

        supplier_status = supplier.status.value if hasattr(supplier.status, 'value') else str(supplier.status)
        if supplier_status != "active":
            return False, "المورد غير نشط في النظام حالياً."

        if existing:
            existing.status = "pending"
            existing.decided_by = None
            existing.decided_at = None
            existing.status_reason = None
        else:
            new_relation = SupplierRelation(
                company_id=company_id,
                supplier_id=supplier_id,
                status="pending"
            )
            session.add(new_relation)

        await session.commit()

    # Notify supplier users (outside the RLS session — they belong to supplier side)
    try:
        async with get_session(silent=True) as session:
            stmt = select(User).where(
                User.supplier_id == supplier_id,
                User.status == "active"
            )
            result = await session.execute(stmt)
            supplier_users = result.scalars().all()

        notified = False
        for supplier_user in supplier_users:
            try:
                await bot.send_message(
                    chat_id=supplier_user.telegram_user_id,
                    text=(
                        f"🏪 <b>طلب ربط جديد من شركة</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📩 تود إحدى الشركات ربط حسابها بك كمورد في النظام.\n\n"
                        f"👤 دعاك: <b>{invited_by.full_name}</b>\n\n"
                        f"💡 استخدم /start للدخول إلى لوحة تحكم المورد والرد على الطلب."
                    ),
                    parse_mode="HTML"
                )
                notified = True
            except Exception as e:
                logger.warning(
                    "Could not notify supplier user | supplier_user_id=%s | error=%s",
                    supplier_user.id,
                    e,
                )

        if not notified:
            logger.warning("No supplier users notified | supplier_id=%s", supplier_id)
    except Exception as e:
        logger.warning(
            "Could not fetch supplier users for notification | error=%s", e
        )

    return True, (
        f"✅ <b>تم إرسال طلب الربط بنجاح!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏭 المورد: <b>{supplier.name}</b>\n"
        f"📊 الحالة: <b>معلق (بانتظار موافقة المورد)</b>\n\n"
        f"📨 تم إشعار المورد بطلب الربط.\n"
        f"⏳ ستظهر العلاقة في قائمة الموردين بعد موافقة المورد."
    )
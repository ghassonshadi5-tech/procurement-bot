"""
Request handlers - create, view, and review purchase requests.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

KEY ARCHITECTURAL CHANGES:
- Multi-line requests (purchase_request_lines).
- NO PRICES in the request (handled later in RFQ/Quotation phase).
- NO supplier selection during request creation.
- 12-state lifecycle (draft -> submitted -> under_review -> approved/rejected/needs_revision).
- Fully async using purchase_request_service and get_session().
- RBAC enforcement via permissions module.

CHANGE LOG:
- UOM selection now shows buttons for existing units + "Write custom unit" option.
- Fixed issue where request summary would hang (company_id was None from middleware).
- Fixed UOM display to use code field (name_ar doesn't exist in current schema).
- UPDATED: Allowed Accountants to create new purchase requests per business requirement.

CHANGE LOG (this revision):
- FIXED (security/logic): cancel_by_text now uses explicit StateFilter instead
  of StateFilter("*"), preventing interference with other FSM flows.
- FIXED (security): _notify_managers_new_request now uses
  get_session(company_id=...) instead of silent=True (RLS-enforced).
"""
from typing import List, Optional, Dict, Any
from decimal import Decimal, InvalidOperation

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.core.logger import get_logger
from app.core import permissions
from app.database.database import get_session
from app.database.models.unit_of_measure import UnitOfMeasure
from app.database.models.user import User
from app.database.models.enums import RequestStatus
from app.services.purchase_request_service import purchase_request_service

logger = get_logger(__name__)
router = Router(name="requests")


# ============================================================================
# FSM States
# ============================================================================
class RequestFlow(StatesGroup):
    """States for creating a multi-line purchase request."""
    waiting_for_title = State()
    waiting_for_item_name = State()
    waiting_for_quantity = State()
    waiting_for_uom = State()
    waiting_for_uom_custom = State()
    waiting_for_specs = State()
    waiting_for_add_more = State()
    confirming = State()
    waiting_for_submit_confirmation = State()


class ReviewFlow(StatesGroup):
    """States for manager review actions."""
    waiting_for_rejection_reason = State()
    waiting_for_revision_notes = State()


# ============================================================================
# Helpers
# ============================================================================
def get_status_emoji_and_text(status: str) -> str:
    """Return emoji and Arabic text for request status."""
    status_map = {
        "draft": "📝 مسودة",
        "submitted": "📨 مُرسل للمراجعة",
        "under_review": "👁️ قيد المراجعة",
        "needs_revision": "🔄 يحتاج تعديل",
        "approved": "✅ معتمد",
        "rejected": "❌ مرفوض",
        "cancelled": "🚫 ملغى",
        "rfq_created": "📢 تم طرحه للمنافسة",
        "awarded": "🏆 تمت الترسية",
        "received": "📦 تم الاستلام",
        "closed": "🏁 مغلق",
        "archived": "🗄️ أرشيف",
    }
    return status_map.get(status, status)


async def get_uom_keyboard() -> InlineKeyboardMarkup:
    """Fetch UOMs from DB and build an inline keyboard with custom option."""
    async with get_session(silent=True) as session:
        stmt = select(UnitOfMeasure).order_by(UnitOfMeasure.code)
        result = await session.execute(stmt)
        uoms = result.scalars().all()

    buttons = []
    row = []

    # Use code directly since name_ar doesn't exist in current schema
    for uom in uoms:
        text = uom.code
        row.append(InlineKeyboardButton(text=text, callback_data=f"uom:{uom.id}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Custom UOM option (always visible)
    buttons.append([InlineKeyboardButton(text="✏️ كتابة وحدة جديدة", callback_data="uom:custom")])
    buttons.append([InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data="cancel_request_flow")])

    if not uoms:
        buttons.insert(0, [InlineKeyboardButton(
            text="⚠️ لا توجد وحدات قياس - اكتب وحدة جديدة",
            callback_data="uom:custom"
        )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_lines_summary(lines: List[Dict[str, Any]]) -> str:
    """Format the lines list for the confirmation screen."""
    text = ""
    for i, line in enumerate(lines, 1):
        text += (
            f"\n<b>{i}. {line['item_name']}</b>\n"
            f"   🔢 الكمية: {line['quantity']}\n"
            f"   📏 الوحدة: {line['uom_text']}\n"
        )
        if line.get('specs'):
            text += f"   📝 ملاحظات: {line['specs']}\n"
    return text


# ============================================================================
# Cancel Handlers (Text & Button)
# ============================================================================
# Explicit FSM states for the cancel-by-text handler — prevents this handler
# from intercepting "إلغاء" during unrelated FSM flows (e.g. manager.py).
_REQUEST_CANCEL_STATES = (
    RequestFlow.waiting_for_title,
    RequestFlow.waiting_for_item_name,
    RequestFlow.waiting_for_quantity,
    RequestFlow.waiting_for_uom,
    RequestFlow.waiting_for_uom_custom,
    RequestFlow.waiting_for_specs,
    RequestFlow.waiting_for_add_more,
    RequestFlow.confirming,
    RequestFlow.waiting_for_submit_confirmation,
)


@router.message(StateFilter(*_REQUEST_CANCEL_STATES), F.text == "إلغاء")
async def cancel_by_text(message: Message, state: FSMContext):
    """Cancel any active request-creation flow by typing 'إلغاء'."""
    await state.clear()
    await message.answer(
        "❌ <b>تم إلغاء العملية</b>\n"
        "لم يتم تنفيذ أي تغيير.\n"
        "يمكنك البدء من جديد باستخدام /new_request",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "cancel_request_flow")
async def callback_cancel_flow(callback: CallbackQuery, state: FSMContext):
    """Cancel request creation via button."""
    await state.clear()
    await callback.message.edit_text(
        "❌ <b>تم إلغاء إنشاء الطلب</b>\n"
        "لم يتم حفظ أي بيانات.",
        parse_mode="HTML"
    )
    await callback.answer("تم الإلغاء")


# ============================================================================
# Command: /new_request - Start creating a new purchase request
# ============================================================================
@router.message(Command("new_request"))
@router.message(F.text.in_(["🆕 طلب شراء جديد", "🆕 طلب جديد"]))
async def cmd_new_request(message: Message, user: Optional[User], roles: List[str], state: FSMContext):
    """Handle /new_request command. Check permissions and start FSM."""
    if not user:
        await message.answer("⛔ يجب التسجيل أولاً. استخدم /start.")
        return

    # Requester, Manager, Owner, AND Accountant can create requests
    if not (
        permissions.is_requester(roles) or
        permissions.is_manager(roles) or
        permissions.is_company_owner(roles) or
        permissions.is_accountant(roles)
    ):
        await message.answer("⛔ ليس لديك صلاحية إنشاء طلبات شراء.")
        return

    await state.clear()
    await state.update_data(lines=[])

    await message.answer(
        "🆕 <b>إنشاء طلب شراء جديد</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 الرجاء إدخال <b>عنوان الطلب</b> أو ملخصه:\n"
        "(مثال: مستلزمات مكتبية لشهر أكتوبر)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_request_flow")]
        ])
    )
    await state.set_state(RequestFlow.waiting_for_title)


# ============================================================================
# FSM Step 1: Title
# ============================================================================
@router.message(RequestFlow.waiting_for_title)
async def process_title(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ الرجاء إرسال نص فقط.")
        return

    title = message.text.strip()
    if len(title) < 3:
        await message.answer("❌ العنوان قصير جداً. (3 أحرف على الأقل)")
        return

    await state.update_data(title=title)
    await message.answer(
        f"✅ <b>العنوان:</b> {title}\n\n"
        f"📦 الرجاء إدخال <b>اسم الصنف الأول</b>:\n"
        f"(مثال: ورق طباعة A4)",
        parse_mode="HTML"
    )
    await state.set_state(RequestFlow.waiting_for_item_name)


# ============================================================================
# FSM Step 2: Item Name
# ============================================================================
@router.message(RequestFlow.waiting_for_item_name)
async def process_item_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ الرجاء إرسال نص فقط.")
        return

    item_name = message.text.strip()
    if len(item_name) < 2:
        await message.answer("❌ اسم الصنف قصير جداً.")
        return

    await state.update_data(current_item_name=item_name)
    await message.answer(
        f"✅ <b>الصنف:</b> {item_name}\n\n"
        f"🔢 الرجاء إدخال <b>الكمية المطلوبة</b>:\n"
        f"(مثال: 10 أو 5.5)",
        parse_mode="HTML"
    )
    await state.set_state(RequestFlow.waiting_for_quantity)


# ============================================================================
# FSM Step 3: Quantity
# ============================================================================
@router.message(RequestFlow.waiting_for_quantity)
async def process_quantity(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ الرجاء إرسال رقم فقط.")
        return

    try:
        qty_str = message.text.strip().replace(',', '.')
        quantity = Decimal(qty_str)
        if quantity <= 0:
            await message.answer("❌ الكمية يجب أن تكون أكبر من 0.")
            return
    except InvalidOperation:
        await message.answer("❌ الرجاء إدخال رقم صحيح. (مثال: 10 أو 5.5)")
        return

    await state.update_data(current_quantity=quantity)

    # Show UOM keyboard with existing units + custom option
    kb = await get_uom_keyboard()
    await message.answer(
        f"✅ <b>الكمية:</b> {quantity}\n\n"
        f"📏 الرجاء اختيار <b>وحدة القياس</b>:",
        reply_markup=kb
    )
    await state.set_state(RequestFlow.waiting_for_uom)


# ============================================================================
# FSM Step 4: UOM Selection (Button or Custom)
# ============================================================================
@router.callback_query(RequestFlow.waiting_for_uom, F.data.startswith("uom:"))
async def process_uom(callback: CallbackQuery, state: FSMContext):
    uom_value = callback.data.split(":", 1)[1]

    if uom_value == "custom":
        await callback.message.edit_text(
            "✏️ <b>كتابة وحدة قياس جديدة</b>\n\n"
            "اكتب اسم الوحدة المطلوبة:\n"
            "(مثال: كرتون، دزينة، طن...)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_request_flow")]
            ])
        )
        await state.set_state(RequestFlow.waiting_for_uom_custom)
        await callback.answer()
        return

    # User selected existing UOM from buttons
    uom_id = int(uom_value)

    async with get_session(silent=True) as session:
        uom = await session.get(UnitOfMeasure, uom_id)
        uom_text = uom.code if uom else "وحدة"

    await state.update_data(current_uom_id=uom_id, current_uom_text=uom_text)

    skip_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏭️ تخطي (بدون ملاحظات)", callback_data="skip_specs"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_request_flow")
        ]
    ])

    await callback.message.edit_text(
        f"✅ <b>الوحدة:</b> {uom_text}\n\n"
        f"📝 هل تريد إضافة <b>مواصفات أو ملاحظات</b> لهذا البند؟\n"
        f"(مثال: لون أبيض، ماركة HP)\n"
        f"اكتب الملاحظات أو اضغط 'تخطي':",
        reply_markup=skip_kb
    )
    await state.set_state(RequestFlow.waiting_for_specs)
    await callback.answer()


# ============================================================================
# FSM Step 4b: Custom UOM Text Input
# ============================================================================
@router.message(RequestFlow.waiting_for_uom_custom)
async def process_uom_custom_text(message: Message, state: FSMContext):
    """Process custom UOM text input from user."""
    if not message.text:
        await message.answer("❌ الرجاء إرسال نص فقط.")
        return

    uom_text = message.text.strip()

    if len(uom_text) < 1:
        await message.answer("❌ وحدة القياس لا يمكن أن تكون فارغة. الرجاء كتابة اسم الوحدة.")
        return

    if len(uom_text) > 50:
        await message.answer("❌ وحدة القياس طويلة جداً. (50 حرف كحد أقصى)")
        return

    # Save custom UOM (no DB ID)
    await state.update_data(
        current_uom_id=None,
        current_uom_text=uom_text
    )

    skip_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏭️ تخطي (بدون ملاحظات)", callback_data="skip_specs"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_request_flow")
        ]
    ])

    await message.answer(
        f"✅ <b>الوحدة:</b> {uom_text}\n\n"
        f"📝 هل تريد إضافة <b>مواصفات أو ملاحظات</b> لهذا البند؟\n"
        f"(مثال: لون أبيض، ماركة HP)\n"
        f"اكتب الملاحظات أو اضغط 'تخطي':",
        reply_markup=skip_kb,
        parse_mode="HTML"
    )
    await state.set_state(RequestFlow.waiting_for_specs)


# ============================================================================
# FSM Step 5: Specs (Optional)
# ============================================================================
@router.message(RequestFlow.waiting_for_specs)
async def process_specs(message: Message, state: FSMContext):
    specs = message.text.strip() if message.text else None
    await _finalize_line_and_ask_for_more(message, state, specs)


@router.callback_query(RequestFlow.waiting_for_specs, F.data == "skip_specs")
async def callback_skip_specs(callback: CallbackQuery, state: FSMContext):
    await _finalize_line_and_ask_for_more(callback.message, state, None)
    await callback.answer()


async def _finalize_line_and_ask_for_more(message: Message, state: FSMContext, specs: Optional[str]):
    """Save the current line to the list and ask if user wants to add more."""
    data = await state.get_data()
    lines = data.get("lines", [])

    new_line = {
        "item_name": data["current_item_name"],
        "quantity": data["current_quantity"],
        "uom_id": data.get("current_uom_id"),
        "uom_text": data["current_uom_text"],
        "specs": specs,
    }
    lines.append(new_line)
    await state.update_data(lines=lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ إضافة بند آخر", callback_data="add_another_line"),
            InlineKeyboardButton(text="✅ إنهاء ومراجعة الطلب", callback_data="finish_lines")
        ],
        [InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data="cancel_request_flow")]
    ])

    await message.answer(
        f"✅ تم إضافة البند: <b>{new_line['item_name']}</b>\n\n"
        f"📋 عدد البنود حتى الآن: <b>{len(lines)}</b>\n"
        f"ماذا تريد أن تفعل؟",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(RequestFlow.waiting_for_add_more)


# ============================================================================
# FSM Step 6: Add More or Finish
# ============================================================================
@router.callback_query(RequestFlow.waiting_for_add_more, F.data == "add_another_line")
async def callback_add_another(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"📦 الرجاء إدخال <b>اسم الصنف التالي</b>:",
        parse_mode="HTML"
    )
    await state.set_state(RequestFlow.waiting_for_item_name)
    await callback.answer()


@router.callback_query(RequestFlow.waiting_for_add_more, F.data == "finish_lines")
async def callback_finish_lines(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    title = data.get("title", "بدون عنوان")
    lines = data.get("lines", [])

    summary = (
        f"📋 <b>ملخص طلب الشراء</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>العنوان:</b> {title}\n"
        f"📦 <b>البنود ({len(lines)}):</b>"
        f"{format_lines_summary(lines)}\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"هل تريد حفظ الطلب كـ <b>مسودة</b>؟"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ حفظ كمسودة", callback_data="confirm_save_draft"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_request_flow")
        ]
    ])

    await callback.message.edit_text(summary, reply_markup=kb, parse_mode="HTML")
    await state.set_state(RequestFlow.confirming)
    await callback.answer()


# ============================================================================
# FSM Step 7: Confirm & Save Draft
# ============================================================================
@router.callback_query(RequestFlow.confirming, F.data == "confirm_save_draft")
async def callback_confirm_save(callback: CallbackQuery, user: User, state: FSMContext, company_id: Optional[int]):
    actual_company_id = company_id or user.company_id

    if not actual_company_id:
        await callback.answer("⛔ خطأ: لا يوجد شركة مرتبطة بحسابك.", show_alert=True)
        await state.clear()
        return

    data = await state.get_data()
    title = data["title"]
    lines = data["lines"]

    new_request = await purchase_request_service.create_request(
        company_id=actual_company_id,
        requester_id=user.id,
        title=title,
        lines=lines
    )

    if not new_request:
        await callback.message.edit_text("❌ عذراً، حدث خطأ أثناء إنشاء الطلب.")
        await state.clear()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📨 إرسال للمراجعة الآن", callback_data=f"submit_for_review:{new_request.id}"),
            InlineKeyboardButton(text="📝 الإبقاء كمسودة", callback_data="keep_as_draft")
        ]
    ])

    await callback.message.edit_text(
        f"🎉 <b>تم حفظ الطلب بنجاح كمسودة!</b>\n"
        f"🔢 رقم الطلب: <code>{new_request.id}</code>\n\n"
        f"هل تريد إرساله للمدير للمراجعة الآن؟",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(RequestFlow.waiting_for_submit_confirmation)
    await state.update_data(created_request_id=new_request.id)
    await callback.answer("تم الحفظ")


@router.callback_query(RequestFlow.waiting_for_submit_confirmation, F.data == "keep_as_draft")
async def callback_keep_draft(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✅ تم الإبقاء على الطلب كمسودة.\n"
        "يمكنك إرساله للمراجعة لاحقاً من قائمة طلباتي.",
        parse_mode="HTML"
    )
    await state.clear()
    await callback.answer()


@router.callback_query(RequestFlow.waiting_for_submit_confirmation, F.data.startswith("submit_for_review:"))
async def callback_submit_for_review(callback: CallbackQuery, state: FSMContext, company_id: Optional[int], roles: List[str], user: User):
    actual_company_id = company_id or user.company_id

    if not actual_company_id:
        await callback.answer("⛔ خطأ: لا يوجد شركة مرتبطة بحسابك.", show_alert=True)
        await state.clear()
        return

    request_id = int(callback.data.split(":", 1)[1])

    success = await purchase_request_service.submit_request(request_id, actual_company_id)

    if success:
        await callback.message.edit_text(
            "📨 <b>تم إرسال الطلب للمراجعة بنجاح!</b>\n"
            "سيتم إشعار المدراء المختصين.",
            parse_mode="HTML"
        )
        await _notify_managers_new_request(callback.bot, actual_company_id, request_id, callback.from_user.full_name)
    else:
        await callback.message.edit_text("❌ فشل إرسال الطلب.")

    await state.clear()
    await callback.answer()


# ============================================================================
# Command: /my_requests
# ============================================================================
@router.message(Command("my_requests"))
@router.message(F.text == "📋 طلباتي")
async def cmd_my_requests(message: Message, user: Optional[User], company_id: Optional[int]):
    actual_company_id = company_id or (user.company_id if user else None)

    if not user or not actual_company_id:
        await message.answer("⛔ يجب التسجيل أولاً.")
        return

    requests = await purchase_request_service.get_user_requests(actual_company_id, user.id, limit=10)

    if not requests:
        await message.answer("📭 لا توجد طلبات بعد.\nاستخدم /new_request لإنشاء طلب جديد.")
        return

    response = f"📋 <b>أحدث طلباتك ({len(requests)}):</b>\n"
    for req in requests:
        status_text = get_status_emoji_and_text(req.status.value)
        response += f"\n🔢 <code>{req.id}</code> | {req.title}\n   الحالة: {status_text}\n"

    await message.answer(response, parse_mode="HTML")


# ============================================================================
# Manager Review: Pending Requests
# ============================================================================
@router.message(Command("pending_requests"))
@router.message(F.text.in_(["⏳ للمراجعة", "⏳ الطلبات المعلقة", "📊 طلبات معلقة (مدير)"]))
async def cmd_pending_requests(message: Message, user: Optional[User], roles: List[str], company_id: Optional[int]):
    actual_company_id = company_id or (user.company_id if user else None)

    if not user or not actual_company_id:
        await message.answer("⛔ يجب التسجيل أولاً.")
        return

    if not permissions.can_approve_requests(roles):
        await message.answer("⛔ ليس لديك صلاحية مراجعة الطلبات.")
        return

    requests = await purchase_request_service.get_pending_review_requests(actual_company_id, limit=10)

    if not requests:
        await message.answer("🎉 لا توجد طلبات معلقة للمراجعة حالياً.")
        return

    await message.answer(f"📨 <b>طلبات بانتظار المراجعة ({len(requests)}):</b>", parse_mode="HTML")

    for req in requests:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ اعتماد", callback_data=f"review_approve:{req.id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"review_reject_start:{req.id}")
            ],
            [
                InlineKeyboardButton(text="🔄 طلب تعديل", callback_data=f"review_revision_start:{req.id}")
            ]
        ])

        lines = await purchase_request_service.get_request_lines(req.id, actual_company_id)

        # Fetch requester name
        requester_name = f"ID {req.requester_id}"
        try:
            async with get_session(silent=True) as session:
                requester = await session.get(User, req.requester_id)
                if requester:
                    requester_name = requester.full_name
        except Exception:
            logger.exception("Failed to fetch requester name for request %s", req.id)

        card = (
            f"🔢 <b>رقم الطلب:</b> <code>{req.id}</code>\n"
            f"📝 <b>العنوان:</b> {req.title}\n"
            f"📦 <b>عدد البنود:</b> {len(lines)}\n"
            f"👤 <b>مقدم الطلب:</b> {requester_name}"
        )
        await message.answer(card, reply_markup=kb, parse_mode="HTML")


# ============================================================================
# Manager Actions: Approve
# ============================================================================
@router.callback_query(F.data.startswith("review_approve:"))
async def callback_approve(callback: CallbackQuery, user: User, roles: List[str], company_id: Optional[int]):
    actual_company_id = company_id or user.company_id

    if not actual_company_id:
        await callback.answer("⛔ خطأ: لا يوجد شركة مرتبطة بحسابك.", show_alert=True)
        return

    if not permissions.can_approve_requests(roles):
        await callback.answer("⛔ ليس لديك صلاحية.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])

    # Fetch requester info BEFORE approving (so we can notify them)
    requester_tg_id = None
    requester_name = None
    try:
        async with get_session(silent=True) as session:
            from app.database.models.purchase_request import PurchaseRequest
            pr = await session.get(PurchaseRequest, request_id)
            if pr and pr.requester_id:
                requester = await session.get(User, pr.requester_id)
                if requester:
                    requester_tg_id = requester.telegram_user_id
                    requester_name = requester.full_name
    except Exception:
        logger.exception("Failed to fetch requester for approval notification")

    success = await purchase_request_service.approve_request(request_id, actual_company_id, user.id)

    if success:
        await callback.message.edit_text(
            f"✅ <b>تم اعتماد الطلب {request_id} بنجاح.</b>",
            parse_mode="HTML"
        )

        # Notify requester
        if requester_tg_id:
            try:
                await callback.bot.send_message(
                    chat_id=requester_tg_id,
                    text=(
                        f"✅ <b>تم اعتماد طلب الشراء الخاص بك!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━\n\n"
                        f"🔢 رقم الطلب: <code>{request_id}</code>\n"
                        f"👤 اعتمده: {user.full_name}\n\n"
                        f"🎉 سيتم إنشاء طلب عروض الأسعار (RFQ) قريباً."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                logger.exception("Failed to notify requester about approval")
    else:
        await callback.answer("❌ فشل الاعتماد.", show_alert=True)

    await callback.answer()


# ============================================================================
# Manager Actions: Reject (FSM)
# ============================================================================
@router.callback_query(F.data.startswith("review_reject_start:"))
async def callback_reject_start(callback: CallbackQuery, roles: List[str], state: FSMContext):
    if not permissions.can_approve_requests(roles):
        await callback.answer("⛔ ليس لديك صلاحية.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])
    await state.update_data(reviewing_request_id=request_id)

    await callback.message.answer(
        "❌ <b>رفض الطلب</b>\n"
        "📝 الرجاء إدخال <b>سبب الرفض</b> (إلزامي):",
        parse_mode="HTML"
    )
    await state.set_state(ReviewFlow.waiting_for_rejection_reason)
    await callback.answer()


@router.message(ReviewFlow.waiting_for_rejection_reason)
async def process_rejection_reason(message: Message, user: User, state: FSMContext, company_id: Optional[int]):
    actual_company_id = company_id or user.company_id

    if not actual_company_id:
        await message.answer("⛔ خطأ: لا يوجد شركة مرتبطة بحسابك.")
        await state.clear()
        return

    reason = message.text.strip()
    if not reason:
        await message.answer("❌ السبب إلزامي. الرجاء إدخال نص.")
        return

    data = await state.get_data()
    request_id = data.get("reviewing_request_id")

    success = await purchase_request_service.reject_request(request_id, actual_company_id, user.id, reason)

    if success:
        await message.answer(f"❌ <b>تم رفض الطلب {request_id}.</b>\nالسبب: {reason}", parse_mode="HTML")
    else:
        await message.answer("❌ فشل الرفض.")

    await state.clear()


# ============================================================================
# Manager Actions: Request Revision (FSM)
# ============================================================================
@router.callback_query(F.data.startswith("review_revision_start:"))
async def callback_revision_start(callback: CallbackQuery, roles: List[str], state: FSMContext):
    if not permissions.can_approve_requests(roles):
        await callback.answer("⛔ ليس لديك صلاحية.", show_alert=True)
        return

    request_id = int(callback.data.split(":", 1)[1])
    await state.update_data(reviewing_request_id=request_id)

    await callback.message.answer(
        "🔄 <b>طلب تعديل</b>\n"
        "📝 الرجاء إدخال <b>ملاحظات التعديل</b> للمنشئ (إلزامي):",
        parse_mode="HTML"
    )
    await state.set_state(ReviewFlow.waiting_for_revision_notes)
    await callback.answer()


@router.message(ReviewFlow.waiting_for_revision_notes)
async def process_revision_notes(message: Message, user: User, state: FSMContext, company_id: Optional[int]):
    actual_company_id = company_id or user.company_id

    if not actual_company_id:
        await message.answer("⛔ خطأ: لا يوجد شركة مرتبطة بحسابك.")
        await state.clear()
        return

    notes = message.text.strip()
    if not notes:
        await message.answer("❌ الملاحظات إلزامية.")
        return

    data = await state.get_data()
    request_id = data.get("reviewing_request_id")

    success = await purchase_request_service.request_revision(request_id, actual_company_id, user.id, notes)

    if success:
        await message.answer(f"🔄 <b>تم إعادة الطلب {request_id} للمنشئ للتعديل.</b>", parse_mode="HTML")
    else:
        await message.answer("❌ فشل الإجراء.")

    await state.clear()


# ============================================================================
# Notifications Helper
# ============================================================================
async def _notify_managers_new_request(bot, company_id: int, request_id: int, requester_name: str):
    """Notify managers/owners about a new submitted request."""
    from app.database.models.user_role import UserRole
    from app.database.models.role import Role

    # RLS-enforced session (was silent=True — security fix)
    async with get_session(company_id=company_id) as session:
        stmt = (
            select(User.telegram_user_id)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, UserRole.role_id == Role.id)
            .where(
                User.company_id == company_id,
                Role.code.in_(["manager", "company_owner"])
            )
        )
        result = await session.execute(stmt)
        manager_tg_ids = [row[0] for row in result.all()]

    text = (
        f"🔔 <b>طلب شراء جديد بانتظار المراجعة</b>\n"
        f"🔢 رقم الطلب: <code>{request_id}</code>\n"
        f"👤 مقدم الطلب: {requester_name}\n"
        f"استخدم /pending_requests لمراجعته."
    )

    for tg_id in manager_tg_ids:
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to notify manager %s", tg_id)
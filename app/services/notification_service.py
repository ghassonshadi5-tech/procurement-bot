"""
Notification Service - Manages all system notifications.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Create notification records in the database.
- Send Telegram messages to authorized users.
- Handle deadline reminders, status changes, and decision notifications.
- Ensure idempotency (no duplicate notifications).

Key Rules from Architecture Document:
- Notifications do NOT change business state.
- Notifications do NOT grant permissions.
- Notifications are sent only to authorized users.
- Deadline reminders must not repeat without control.
- All notifications are logged in the notifications table.

IMPROVEMENTS (this revision):
- notify_user() accepts an optional session so callers can batch
  multiple notifications within a single session (fixes N+1 pattern).
- notify_users_by_role() uses DISTINCT on User.id to avoid sending
  duplicate notifications to users with multiple matching roles.
- Telegram message is sent AFTER the notification record is committed,
  then a short UPDATE sets sent_at (avoids holding a long transaction
  open during network I/O).
- Unscoped sessions use silent=True to avoid warning noise.
- Logging uses %s formatting.
- i18n is intentionally deferred (see module docstring note).
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import select, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.notification import Notification
from app.database.models.user import User, UserStatus
from app.database.models.user_role import UserRole
from app.database.models.role import Role
from app.database.models.purchase_request import PurchaseRequest
from app.database.models.rfq import Rfq
from app.database.models.quotation import Quotation
from app.core.logger import get_logger


logger = get_logger(__name__)


# ============================================================================
# Notification Kinds (Constants)
# ============================================================================
class NotificationKind:
    """All notification kinds in the system."""
    # Purchase Request notifications
    REQUEST_SUBMITTED = "request_submitted"
    REQUEST_APPROVED = "request_approved"
    REQUEST_REJECTED = "request_rejected"
    REQUEST_REVISION = "request_revision"
    REQUEST_CANCELLED = "request_cancelled"

    # RFQ notifications
    RFQ_PUBLISHED = "rfq_published"
    RFQ_DEADLINE_REMINDER = "rfq_deadline_reminder"
    RFQ_DEADLINE_REACHED = "rfq_deadline_reached"
    RFQ_CANCELLED = "rfq_cancelled"

    # Quotation notifications
    QUOTATION_SUBMITTED = "quotation_submitted"
    QUOTATION_WITHDRAWN = "quotation_withdrawn"

    # Award notifications
    AWARD_SELECTED = "award_selected"
    AWARD_NOT_SELECTED = "award_not_selected"

    # Supplier/Company notifications
    COMPANY_APPROVED = "company_approved"
    COMPANY_REJECTED = "company_rejected"
    SUPPLIER_APPROVED = "supplier_approved"
    SUPPLIER_REJECTED = "supplier_rejected"

    # Relationship notifications
    RELATION_REQUESTED = "relation_requested"
    RELATION_ACCEPTED = "relation_accepted"
    RELATION_REJECTED = "relation_rejected"
    RELATION_SUSPENDED = "relation_suspended"

    # Goods Receipt notifications
    GOODS_RECEIVED = "goods_received"
    GOODS_VARIANCE = "goods_variance"


class NotificationService:
    """
    Service class for all notification operations.
    """

    # ========================================================================
    # 1. CORE: Create and Send Notification
    # ========================================================================

    @staticmethod
    async def notify_user(
        bot,
        user_id: int,
        kind: str,
        payload: Dict[str, Any],
        company_id: Optional[int] = None,
        message_text: Optional[str] = None,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        """
        Send a notification to a single user.

        TRANSACTION SEMANTICS
        ---------------------
        If `session` is provided, it is used for the User lookup and the
        Notification INSERT. The commit is left to the caller (batch mode).

        If `session` is None, a new session is opened, the notification
        record is committed FIRST, then the Telegram message is sent,
        then a short UPDATE sets sent_at. This avoids holding a DB
        transaction open during network I/O.

        Args:
            bot: The Aiogram Bot instance.
            user_id: The internal user ID (not Telegram ID).
            kind: The notification kind.
            payload: Additional data for the notification.
            company_id: The company scope (optional).
            message_text: The formatted Telegram message text.
            session: Optional caller-managed session (for batching).

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        # --- Resolve context manager for standalone mode ---
        def _open_ctx():
            if company_id is not None:
                return get_session(company_id=company_id)
            return get_session(silent=True)

        # --- Shared inner: build the Notification row ---
        async def _create_record(sess: AsyncSession) -> Optional[Notification]:
            stmt = select(User).where(User.id == user_id)
            user = (await sess.execute(stmt)).scalar_one_or_none()

            if not user or user.status != UserStatus.active:
                logger.warning(
                    "Cannot notify user | user_id=%s | reason=not_found_or_inactive",
                    user_id,
                )
                return None

            notification = Notification(
                user_id=user_id,
                company_id=company_id,
                kind=kind,
                payload=payload,
            )
            sess.add(notification)
            await sess.flush()
            return notification, user  # type: ignore[return-value]

        # --- Shared inner: send Telegram message ---
        async def _send(tg_id: int, text: str) -> bool:
            try:
                await bot.send_message(
                    chat_id=tg_id,
                    text=text,
                    parse_mode="HTML",
                )
                return True
            except Exception:
                logger.exception(
                    "Failed to send Telegram notification | user_id=%s | kind=%s",
                    user_id,
                    kind,
                )
                return False

        resolved_text = message_text or NotificationService._build_default_message(
            kind, payload
        )

        # ------------------------------------------------------------------
        # Mode A: Caller-managed session (batch)
        # ------------------------------------------------------------------
        if session is not None:
            result = await _create_record(session)
            if result is None:
                return False
            _, user = result

            sent_ok = await _send(user.telegram_user_id, resolved_text)
            if sent_ok:
                # Mark sent_at within the caller's session (still uncommitted).
                notification = result[0]
                notification.sent_at = datetime.now(timezone.utc)

            logger.info(
                "Notification queued | user_id=%s | kind=%s | sent=%s",
                user_id,
                kind,
                sent_ok,
            )
            return sent_ok

        # ------------------------------------------------------------------
        # Mode B: Standalone session (short commit, then I/O, then update)
        # ------------------------------------------------------------------
        # Step 1: Insert the notification record (short transaction).
        async with _open_ctx() as sess:
            result = await _create_record(sess)
            if result is None:
                await sess.commit()  # nothing was added, commit is a no-op
                return False
            notification, user = result
            notification_id = notification.id
            tg_id = user.telegram_user_id
            await sess.commit()

        # Step 2: Send the Telegram message OUTSIDE any DB transaction.
        sent_ok = await _send(tg_id, resolved_text)

        # Step 3: Update sent_at in a fresh short transaction.
        if sent_ok:
            async with _open_ctx() as sess:
                stmt = select(Notification).where(Notification.id == notification_id)
                notif = (await sess.execute(stmt)).scalar_one_or_none()
                if notif is not None:
                    notif.sent_at = datetime.now(timezone.utc)
                    await sess.commit()

        logger.info(
            "Notification sent | user_id=%s | kind=%s | sent=%s",
            user_id,
            kind,
            sent_ok,
        )
        return sent_ok

    @staticmethod
    async def notify_users_by_role(
        bot,
        company_id: int,
        role_codes: List[str],
        kind: str,
        payload: Dict[str, Any],
        message_text: Optional[str] = None,
        exclude_user_id: Optional[int] = None,
    ) -> int:
        """
        Send a notification to all users with specific roles in a company.

        Uses DISTINCT on User.id so a user with multiple matching roles
        receives the notification only once.

        Args:
            bot: The Aiogram Bot instance.
            company_id: The company scope.
            role_codes: List of role codes to notify.
            kind: The notification kind.
            payload: Additional data.
            message_text: The formatted message.
            exclude_user_id: User to exclude.

        Returns:
            Number of successfully notified users.
        """
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(User)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, UserRole.role_id == Role.id)
                .where(
                    User.company_id == company_id,
                    User.status == UserStatus.active,
                    Role.code.in_(role_codes),
                )
                .distinct()
            )

            if exclude_user_id:
                stmt = stmt.where(User.id != exclude_user_id)

            result = await session.execute(stmt)
            users = result.scalars().all()

        notified_count = 0
        for user in users:
            success = await NotificationService.notify_user(
                bot=bot,
                user_id=user.id,
                kind=kind,
                payload=payload,
                company_id=company_id,
                message_text=message_text,
            )
            if success:
                notified_count += 1

        return notified_count

    # ========================================================================
    # 2. PURCHASE REQUEST NOTIFICATIONS
    # ========================================================================

    @staticmethod
    async def notify_request_submitted(
        bot,
        company_id: int,
        request_id: int,
        requester_id: int,
        requester_name: str,
        title: str,
    ) -> int:
        """Notify managers/owners when a new request is submitted."""
        message_text = (
            f"🔔 <b>طلب شراء جديد بانتظار المراجعة</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 رقم الطلب: <code>{request_id}</code>\n"
            f"📝 العنوان: {title}\n"
            f"👤 مقدم الطلب: {requester_name}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"استخدم /pending_requests لمراجعته."
        )

        return await NotificationService.notify_users_by_role(
            bot=bot,
            company_id=company_id,
            role_codes=["manager", "company_owner"],
            kind=NotificationKind.REQUEST_SUBMITTED,
            payload={
                "request_id": request_id,
                "requester_id": requester_id,
                "title": title,
            },
            message_text=message_text,
            exclude_user_id=requester_id,
        )

    @staticmethod
    async def notify_request_decision(
        bot,
        company_id: int,
        request_id: int,
        requester_id: int,
        decision: str,  # "approved", "rejected", "revision"
        decision_note: Optional[str] = None,
        decider_name: Optional[str] = None,
    ) -> bool:
        """Notify the requester about the decision on their request."""
        if decision == "approved":
            emoji = "✅"
            title = "تمت الموافقة على طلبك"
        elif decision == "rejected":
            emoji = "❌"
            title = "تم رفض طلبك"
        else:
            emoji = "🔄"
            title = "طلبك يحتاج إلى تعديل"

        message_text = (
            f"{emoji} <b>{title}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 رقم الطلب: <code>{request_id}</code>\n"
        )

        if decision_note:
            message_text += f"📝 الملاحظات: {decision_note}\n"
        if decider_name:
            message_text += f"👤 بواسطة: {decider_name}\n"

        return await NotificationService.notify_user(
            bot=bot,
            user_id=requester_id,
            kind=f"request_{decision}",
            payload={
                "request_id": request_id,
                "decision": decision,
                "note": decision_note,
            },
            company_id=company_id,
            message_text=message_text,
        )

    # ========================================================================
    # 3. RFQ NOTIFICATIONS
    # ========================================================================

    @staticmethod
    async def notify_rfq_published_to_suppliers(
        bot,
        rfq_id: int,
        company_id: int,
        supplier_ids: List[int],
        deadline: datetime,
        lines_count: int,
    ) -> int:
        """
        Notify invited suppliers when an RFQ is published.
        Sends to supplier users who can respond to RFQs.
        """
        from app.database.models.supplier import Supplier

        notified_count = 0

        async with get_session(silent=True) as session:
            for supplier_id in supplier_ids:
                # Fetch supplier name
                stmt_supplier = select(Supplier).where(Supplier.id == supplier_id)
                supplier = (await session.execute(stmt_supplier)).scalar_one_or_none()
                supplier_name = supplier.name if supplier else f"مورد #{supplier_id}"

                # Fetch supplier users who can respond
                stmt_users = (
                    select(User)
                    .join(UserRole, UserRole.user_id == User.id)
                    .join(Role, UserRole.role_id == Role.id)
                    .where(
                        User.supplier_id == supplier_id,
                        User.status == UserStatus.active,
                        Role.code.in_(
                            [
                                "supplier_owner",
                                "supplier_manager",
                                "supplier_employee",
                            ]
                        ),
                    )
                    .distinct()
                )
                result = await session.execute(stmt_users)
                supplier_users = result.scalars().all()

                message_text = (
                    f"📨 <b>طلب عروض أسعار جديد</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"🔢 رقم المنافسة: <code>{rfq_id}</code>\n"
                    f"📦 عدد البنود: {lines_count}\n"
                    f"⏰ المهلة النهائية: {deadline.strftime('%Y-%m-%d %H:%M')} UTC\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"استخدم /my_rfqs لعرض التفاصيل وتقديم عرضك."
                )

                for user in supplier_users:
                    success = await NotificationService.notify_user(
                        bot=bot,
                        user_id=user.id,
                        kind=NotificationKind.RFQ_PUBLISHED,
                        payload={
                            "rfq_id": rfq_id,
                            "company_id": company_id,
                            "supplier_id": supplier_id,
                            "deadline": deadline.isoformat(),
                        },
                        message_text=message_text,
                    )
                    if success:
                        notified_count += 1

        logger.info(
            "RFQ %s published: notified %s supplier users", rfq_id, notified_count
        )
        return notified_count

    @staticmethod
    async def notify_rfq_deadline_reached(
        bot,
        rfq_id: int,
        company_id: int,
        quotations_count: int,
    ) -> int:
        """
        Notify company managers that the RFQ deadline has been reached.
        Idempotent: checks if notification was already sent.
        """
        async with get_session(company_id=company_id) as session:
            stmt_check = (
                select(Notification)
                .where(
                    Notification.kind == NotificationKind.RFQ_DEADLINE_REACHED,
                    Notification.payload["rfq_id"].as_integer() == rfq_id,
                )
                .limit(1)
            )
            existing = (await session.execute(stmt_check)).scalar_one_or_none()

            if existing:
                logger.info(
                    "Deadline notification for RFQ %s already sent. Skipping.",
                    rfq_id,
                )
                return 0

        message_text = (
            f"⏰ <b>انتهت مهلة المنافسة</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 رقم المنافسة: <code>{rfq_id}</code>\n"
            f"📄 عدد العروض المستلمة: {quotations_count}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
        )

        if quotations_count > 0:
            message_text += "✅ يمكنك الآن مراجعة العروض واختيار العرض المناسب."
        else:
            message_text += "⚠️ لم يتم استلام أي عروض. يمكنك إعادة طرح المنافسة."

        return await NotificationService.notify_users_by_role(
            bot=bot,
            company_id=company_id,
            role_codes=["manager", "company_owner"],
            kind=NotificationKind.RFQ_DEADLINE_REACHED,
            payload={"rfq_id": rfq_id, "quotations_count": quotations_count},
            message_text=message_text,
        )

    @staticmethod
    async def notify_rfq_cancelled(
        bot,
        rfq_id: int,
        company_id: int,
        supplier_ids: List[int],
        reason: Optional[str] = None,
    ) -> int:
        """Notify participating suppliers that the RFQ was cancelled."""
        notified_count = 0

        message_text = (
            f"🚫 <b>تم إلغاء المنافسة</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 رقم المنافسة: <code>{rfq_id}</code>\n"
        )
        if reason:
            message_text += f"📝 السبب: {reason}\n"
        message_text += "━━━━━━━━━━━━━━━━━━━\nنعتذر عن أي إزعاج."

        async with get_session(silent=True) as session:
            for supplier_id in supplier_ids:
                stmt_users = (
                    select(User)
                    .join(UserRole, UserRole.user_id == User.id)
                    .join(Role, UserRole.role_id == Role.id)
                    .where(
                        User.supplier_id == supplier_id,
                        User.status == UserStatus.active,
                        Role.code.in_(["supplier_owner", "supplier_manager"]),
                    )
                    .distinct()
                )
                result = await session.execute(stmt_users)
                users = result.scalars().all()

                for user in users:
                    success = await NotificationService.notify_user(
                        bot=bot,
                        user_id=user.id,
                        kind=NotificationKind.RFQ_CANCELLED,
                        payload={"rfq_id": rfq_id, "reason": reason},
                        message_text=message_text,
                    )
                    if success:
                        notified_count += 1

        return notified_count

    # ========================================================================
    # 4. AWARD NOTIFICATIONS
    # ========================================================================

    @staticmethod
    async def notify_award_result(
        bot,
        rfq_id: int,
        company_id: int,
        winning_supplier_id: int,
        all_supplier_ids: List[int],
        request_title: str,
    ) -> int:
        """
        Notify the winning supplier and all other participants
        about the award result.
        """
        notified_count = 0

        async with get_session(silent=True) as session:
            for supplier_id in all_supplier_ids:
                is_winner = supplier_id == winning_supplier_id

                if is_winner:
                    kind = NotificationKind.AWARD_SELECTED
                    message_text = (
                        f"🏆 <b>تهانينا! تم اختيار عرضك</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🔢 رقم المنافسة: <code>{rfq_id}</code>\n"
                        f"📝 الطلب: {request_title}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"سيتم التواصل معك لتنفيذ الطلب."
                    )
                else:
                    kind = NotificationKind.AWARD_NOT_SELECTED
                    message_text = (
                        f"📋 <b>نتيجة المنافسة</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🔢 رقم المنافسة: <code>{rfq_id}</code>\n"
                        f"📝 الطلب: {request_title}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"لم يتم اختيار عرضك هذه المرة.\n"
                        f"شكراً لمشاركتك، ونتمنى لك حظاً أفضل في المنافسات القادمة."
                    )

                stmt_users = (
                    select(User)
                    .join(UserRole, UserRole.user_id == User.id)
                    .join(Role, UserRole.role_id == Role.id)
                    .where(
                        User.supplier_id == supplier_id,
                        User.status == UserStatus.active,
                        Role.code.in_(["supplier_owner", "supplier_manager"]),
                    )
                    .distinct()
                )
                result = await session.execute(stmt_users)
                users = result.scalars().all()

                for user in users:
                    success = await NotificationService.notify_user(
                        bot=bot,
                        user_id=user.id,
                        kind=kind,
                        payload={
                            "rfq_id": rfq_id,
                            "supplier_id": supplier_id,
                            "is_winner": is_winner,
                        },
                        message_text=message_text,
                    )
                    if success:
                        notified_count += 1

        logger.info(
            "Award notifications sent for RFQ %s: %s users notified",
            rfq_id,
            notified_count,
        )
        return notified_count

    # ========================================================================
    # 5. COMPANY/SUPPLIER REGISTRATION NOTIFICATIONS
    # ========================================================================

    @staticmethod
    async def notify_company_registration_result(
        bot,
        user_telegram_id: int,
        company_name: str,
        approved: bool,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Notify the company creator about registration result.

        NOTE: This is a pre-registration notification — the user may not
        yet have a DB record. We do NOT create a notification entry in
        this case (there is no user_id to attach it to).
        """
        if approved:
            message_text = (
                f"🎉 <b>تم اعتماد شركتك بنجاح!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 الشركة: <b>{company_name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"يمكنك الآن البدء بإنشاء طلبات الشراء ودعوة موظفيك.\n"
                f"استخدم /start للوصول إلى لوحة التحكم."
            )
        else:
            message_text = (
                f"❌ <b>تم رفض طلب تسجيل شركتك</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 الشركة: <b>{company_name}</b>\n"
            )
            if reason:
                message_text += f"📝 السبب: {reason}\n"
            message_text += "━━━━━━━━━━━━━━━━━━━\nيمكنك التواصل مع الدعم للاستفسار."

        try:
            await bot.send_message(
                chat_id=user_telegram_id,
                text=message_text,
                parse_mode="HTML",
            )
            return True
        except Exception:
            logger.exception(
                "Failed to notify company registration result | tg_id=%s",
                user_telegram_id,
            )
            return False

    # ========================================================================
    # 6. GOODS RECEIPT NOTIFICATIONS
    # ========================================================================

    @staticmethod
    async def notify_goods_received(
        bot,
        company_id: int,
        receipt_id: int,
        request_title: str,
        has_variance: bool,
        variance_summary: Optional[str] = None,
    ) -> int:
        """Notify managers about goods receipt confirmation."""
        message_text = (
            f"📦 <b>تم تأكيد استلام البضائع</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 رقم الاستلام: <code>{receipt_id}</code>\n"
            f"📝 الطلب: {request_title}\n"
        )

        if has_variance:
            message_text += (
                f"⚠️ <b>يوجد فروقات في الكميات:</b>\n{variance_summary}\n"
            )
        else:
            message_text += "✅ تم الاستلام بالكامل دون فروقات.\n"

        return await NotificationService.notify_users_by_role(
            bot=bot,
            company_id=company_id,
            role_codes=["manager", "company_owner"],
            kind=NotificationKind.GOODS_RECEIVED,
            payload={"receipt_id": receipt_id, "has_variance": has_variance},
            message_text=message_text,
        )

    # ========================================================================
    # 7. HELPER: Build Default Message
    # ========================================================================

    @staticmethod
    def _build_default_message(kind: str, payload: Dict[str, Any]) -> str:
        """Build a default message for a notification kind."""
        messages = {
            NotificationKind.REQUEST_SUBMITTED: "🔔 تم إرسال طلب شراء جديد للمراجعة.",
            NotificationKind.REQUEST_APPROVED: "✅ تمت الموافقة على طلبك.",
            NotificationKind.REQUEST_REJECTED: "❌ تم رفض طلبك.",
            NotificationKind.REQUEST_REVISION: "🔄 طلبك يحتاج إلى تعديل.",
            NotificationKind.RFQ_PUBLISHED: "📨 طلب عروض أسعار جديد متاح.",
            NotificationKind.RFQ_DEADLINE_REACHED: "⏰ انتهت مهلة المنافسة.",
            NotificationKind.QUOTATION_SUBMITTED: "📄 تم استلام عرض جديد.",
            NotificationKind.AWARD_SELECTED: "🏆 تم اختيار عرضك!",
            NotificationKind.AWARD_NOT_SELECTED: "📋 نتيجة المنافسة: لم يتم اختيار عرضك.",
            NotificationKind.GOODS_RECEIVED: "📦 تم تأكيد استلام البضائع.",
        }
        return messages.get(kind, f"🔔 إشعار: {kind}")

    # ========================================================================
    # 8. GET USER NOTIFICATIONS (For display)
    # ========================================================================

    @staticmethod
    async def get_user_notifications(
        user_id: int,
        limit: int = 20,
        unread_only: bool = False,
    ) -> List[Notification]:
        """Get notifications for a user."""
        async with get_session(silent=True) as session:
            stmt = select(Notification).where(Notification.user_id == user_id)

            if unread_only:
                stmt = stmt.where(Notification.read_at.is_(None))

            stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    async def mark_as_read(notification_id: int, user_id: int) -> bool:
        """Mark a notification as read."""
        async with get_session(silent=True) as session:
            stmt = select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
            notification = (await session.execute(stmt)).scalar_one_or_none()

            if not notification:
                return False

            notification.read_at = datetime.now(timezone.utc)
            await session.commit()
            return True


# Singleton instance
notification_service = NotificationService()
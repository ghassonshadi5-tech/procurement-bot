"""
User Middleware for the Telegram bot (Aiogram 3.x).
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
1. Assign a Correlation ID to every incoming update (end-to-end tracing).
2. Identify the Telegram user and fetch their DB record.
3. Enforce account status (block 'blocked' users).
4. Fetch and inject user roles into the handler data.
5. Inject RLS context (company_id, supplier_id, is_supplier_owner)
   so handlers can easily open isolated DB sessions.
6. Inject the user's preferred language (data["lang"]) for i18n.
7. Log security-relevant events (blocked access, DB failures) with
   extra={"security": True} so they land in logs/security.log.

NOTE: This middleware does NOT create users. New users are handled by /start handlers.

CHANGE LOG:
- Fixed event.answer() on Message → now uses event.reply() for Message events.
"""
import uuid
from typing import Callable, Dict, Any, Awaitable, Union

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject
from sqlalchemy import select

from app.config.settings import settings
from app.database.database import get_session
from app.database.models.user import User, UserStatus
from app.database.models.role import Role
from app.database.models.user_role import UserRole
from app.core.logger import (
    get_logger,
    set_correlation_id,
    clear_correlation_id,
)


logger = get_logger(__name__)


class UserMiddleware(BaseMiddleware):
    """
    Async Middleware that prepares the user context and RLS variables
    for every incoming Telegram update.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Union[Message, CallbackQuery],
        data: Dict[str, Any],
    ) -> Any:
        # ------------------------------------------------------------------
        # 1. Correlation ID for this update (end-to-end tracing)
        # ------------------------------------------------------------------
        cid = uuid.uuid4().hex[:8]
        set_correlation_id(cid)

        try:
            return await self._handle(handler, event, data)
        finally:
            clear_correlation_id()

    async def _handle(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Union[Message, CallbackQuery],
        data: Dict[str, Any],
    ) -> Any:
        # ------------------------------------------------------------------
        # 2. Extract Telegram user
        # ------------------------------------------------------------------
        telegram_user = getattr(event, "from_user", None)
        if not telegram_user:
            return await handler(event, data)

        tg_id = telegram_user.id

        # Initialize default context variables
        data["user"] = None
        data["company_id"] = None
        data["supplier_id"] = None
        data["roles"] = []
        data["is_super_admin"] = False
        data["is_supplier_owner"] = False
        data["lang"] = settings.DEFAULT_LANGUAGE  # default language

        try:
            # --------------------------------------------------------------
            # 3. Initial user lookup (bootstrap — silent, no RLS yet)
            # --------------------------------------------------------------
            async with get_session(silent=True) as session:
                # 3.1 Fetch User
                stmt = select(User).where(User.telegram_user_id == tg_id)
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()

                if not user:
                    # User not found. Handler will decide (e.g. /start flow).
                    return await handler(event, data)

                # 3.2 Check Account Status
                if user.status == UserStatus.blocked:
                    logger.warning(
                        "Blocked user attempted access | tg_id=%s",
                        tg_id,
                        extra={"security": True},
                    )
                    msg = "⛔ حسابك محظور من استخدام النظام. يرجى التواصل مع الإدارة."
                    await _reply_to_event(event, msg, alert=True)
                    return  # Block execution

                # 3.3 Fetch Roles
                stmt_roles = (
                    select(Role.code)
                    .join(UserRole, UserRole.role_id == Role.id)
                    .where(UserRole.user_id == user.id)
                )
                roles_result = await session.execute(stmt_roles)
                roles = [row[0] for row in roles_result.all()]

                # 3.4 Inject Context into Handler Data
                data["user"] = user
                data["roles"] = roles
                data["company_id"] = user.company_id
                data["supplier_id"] = user.supplier_id
                data["lang"] = user.language or settings.DEFAULT_LANGUAGE

                # Determine special flags
                if "super_admin" in roles:
                    data["is_super_admin"] = True

                if "supplier_owner" in roles and user.supplier_id is not None:
                    data["is_supplier_owner"] = True

        except Exception as exc:
            logger.exception(
                "Middleware DB error | tg_id=%s | error=%s",
                tg_id,
                exc,
                extra={"security": True},
            )
            msg = "❌ عذراً، حدث خطأ في الاتصال بقاعدة البيانات. الرجاء المحاولة لاحقاً."
            await _reply_to_event(event, msg, alert=True)
            return  # Don't call handler on DB error

        # Proceed to the actual handler with populated `data`
        return await handler(event, data)


# ============================================================================
# Helper: reply to event regardless of type
# ============================================================================
async def _reply_to_event(
    event: Union[Message, CallbackQuery],
    msg: str,
    alert: bool = False,
) -> None:
    """
    Send a reply to the event, using the appropriate method:
    - CallbackQuery → event.answer(msg, show_alert=alert)
    - Message      → event.reply(msg)
    """
    if isinstance(event, CallbackQuery):
        await event.answer(msg, show_alert=alert)
    elif isinstance(event, Message):
        await event.reply(msg)
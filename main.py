"""
Main entry point for the Procurement Bot.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

KEY CHANGES:
- Added supplier_panel_router to handle Supplier Owner dashboard and actions.
- Added company_owner_router (P6.5) — Company Owner / Manager panel.
- Added supplier_pricing_router (P6.7) — Supplier Pricing Management.
- Fallback router remains the LAST router.
- Safe import with full error reporting for all routers.

IMPROVEMENTS (this revision):
- SCHEDULER_ENABLED is now enforced (was previously only logged).
- drop_pending_updates is conditional (True in dev, False in production).
- MemoryStorage in production logs a WARNING about FSM loss.
- Logging uses %s formatting and logger.exception for clarity.
"""
import asyncio
import traceback

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from app.config.settings import settings
from app.core.logger import get_logger
from app.database.database import dispose_engine
from app.services.user_service import user_service
from app.scheduler import setup_scheduler, shutdown_scheduler


logger = get_logger(__name__)


# =========================================================================
# Helper: Safe Router Import with Full Error Reporting
# =========================================================================
def safe_import_router(module_path: str, router_name: str = "router"):
    """
    Import a router module safely with full error reporting.
    Returns the router or None if failed.
    """
    try:
        module = __import__(module_path, fromlist=[router_name])
        router = getattr(module, router_name)
        logger.info("Loaded router | path=%s", module_path)
        return router
    except Exception:
        logger.exception("Failed to load router | path=%s", module_path)
        return None


# =========================================================================
# Router Imports — with full error reporting
# =========================================================================
start_router = safe_import_router("app.bot.handlers.start")
supplier_registration_router = safe_import_router("app.bot.handlers.supplier_registration")
registration_router = safe_import_router("app.bot.handlers.registration")
super_admin_router = safe_import_router("app.bot.handlers.owner_panel")
invites_router = safe_import_router("app.bot.handlers.invites")
requests_router = safe_import_router("app.bot.handlers.requests")
manager_router = safe_import_router("app.bot.handlers.manager")

# ✅ Company Owner / Manager panel (RFQ, Suppliers, Employees, Reports)
company_owner_router = safe_import_router("app.bot.handlers.company_owner")

# ✅ Supplier Panel (Dashboard, Link Requests, Catalog, Reports)
supplier_panel_router = safe_import_router("app.bot.handlers.supplier_panel")

# ✅ P6.7: Supplier Pricing Management
supplier_pricing_router = safe_import_router("app.bot.handlers.supplier_pricing")

# ✅ Fallback Router — MUST be loaded LAST
fallback_router = safe_import_router("app.bot.handlers.fallback")

# Import Middleware
try:
    from app.bot.middleware.user_middleware import UserMiddleware
    logger.info("UserMiddleware loaded")
except Exception:
    UserMiddleware = None
    logger.exception("CRITICAL: UserMiddleware failed to load")


# =========================================================================
# Lifecycle Hooks
# =========================================================================
async def on_startup(bot: Bot) -> None:
    """Actions to perform when the bot starts."""
    logger.info("Starting Procurement Bot...")

    # 1. Seed Super Admin
    try:
        await user_service.ensure_super_admin(
            telegram_id=settings.SUPER_ADMIN_TELEGRAM_ID,
            full_name="Super Admin",
        )
        logger.info("Super Admin ensured in database.")
    except Exception:
        logger.exception("Failed to ensure Super Admin")

       # 2. Initialize Scheduler (only if enabled)
    if settings.SCHEDULER_ENABLED:
        try:
            setup_scheduler(bot)
            logger.info("Scheduler initialized and started.")
        except Exception:
            logger.exception("Failed to start Scheduler")
    else:
        logger.info("Scheduler disabled by settings (SCHEDULER_ENABLED=False).")

    logger.info("Bot is ready and polling for updates.")


async def on_shutdown(bot: Bot) -> None:
    """Actions to perform when the bot stops."""
    logger.info("Shutting down Procurement Bot...")

    if settings.SCHEDULER_ENABLED:
        try:
            shutdown_scheduler()
        except Exception:
            logger.exception("Error shutting down Scheduler")

    try:
        await dispose_engine()
        logger.info("Database connections closed.")
    except Exception:
        logger.exception("Error disposing database engine")


# =========================================================================
# Main Execution
# =========================================================================
async def main():
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())

    # Warn about FSM loss in production (MemoryStorage does not persist).
    if settings.is_production():
        logger.warning(
            "Using MemoryStorage in production — FSM state will be lost "
            "on every bot restart. Consider RedisStorage or SQLAlchemyStorage "
            "before going live."
        )

    # Register Lifecycle Hooks
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Register Middleware
    if UserMiddleware:
        mw = UserMiddleware()
        dp.message.middleware(mw)
        dp.callback_query.middleware(mw)
        logger.info("Registered UserMiddleware for Message and CallbackQuery")
    else:
        logger.error(
            "UserMiddleware NOT registered — user context will NOT be injected. "
            "Handlers relying on data['user'] / data['roles'] will fail."
        )

    # =========================================================================
    # ROUTING PRIORITY (CORRECT ORDER)
    # =========================================================================

    # P0: Entry Point (Start / Deep Links)
    if start_router:
        dp.include_router(start_router)

    # P1: Supplier Registration (MUST be BEFORE company registration)
    if supplier_registration_router:
        dp.include_router(supplier_registration_router)

    # P2: Company Registration
    if registration_router:
        dp.include_router(registration_router)

    # P3: Super Admin Panel
    if super_admin_router:
        dp.include_router(super_admin_router)

    # P4: Invitations (Deep links and tokens)
    if invites_router:
        dp.include_router(invites_router)

    # P5: Purchase Requests (Creation, Approval, Rejection)
    if requests_router:
        dp.include_router(requests_router)

    # P6: General Manager / Dashboard Operations
    if manager_router:
        dp.include_router(manager_router)

    # P6.5: Company Owner Panel (RFQ list, Suppliers, Employees, Reports)
    if company_owner_router:
        dp.include_router(company_owner_router)

    # P6.6: Supplier Panel (Dashboard, Link Requests, Catalog, Reports)
    if supplier_panel_router:
        dp.include_router(supplier_panel_router)

    # P6.7: Supplier Pricing Management (Prices, Tiers, Activation)
    if supplier_pricing_router:
        dp.include_router(supplier_pricing_router)

    # P8: Fallback Handler — MUST be the LAST router!
    if fallback_router:
        dp.include_router(fallback_router)

    logger.info("Included %d routers. Starting polling...", len(dp.sub_routers))

    # In production, do NOT drop pending updates — the user might have
    # sent valid commands during a brief outage.
    drop_pending = not settings.is_production()

    try:
        await dp.start_polling(bot, drop_pending_updates=drop_pending)
    except Exception:
        logger.exception("Bot stopped unexpectedly")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except SystemExit:
        pass
"""
Scheduler - Background tasks for the Procurement Bot.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Periodically check for expired RFQs and close them.
- Send deadline reminders to company managers.
- Ensure idempotency (no duplicate state changes or notifications).

Key Rules from Architecture & Lifecycle Documents:
- Server time (UTC) is the official reference for deadlines.
- Reaching the deadline closes the RFQ for new submissions, but DOES NOT
  auto-select a winner.
- Notifications must not repeat without control (idempotency).
- The scheduler ONLY calls Services; it does not contain business logic.

IMPROVEMENTS (this revision):
- Uses get_logger(__name__) → correlation IDs and structured logs.
- setup_scheduler(bot) receives the Bot instance and passes it to jobs
  so that notification_service can actually send Telegram messages.
- job_close_expired_rfqs() delegates to rfq_service.close_rfq_system(),
  which handles the state transition, the audit entry, and the
  manager notification.
- Unscoped sessions use silent=True to avoid warning noise.
- Reminder job kept as a placeholder (notifications service hook for
  reminders is not yet implemented — see TODO).
- Logging uses %s formatting.
- Scheduler timezone comes from settings.TIMEZONE.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, func

from app.database.database import get_session
from app.database.models.rfq import Rfq
from app.database.models.notification import Notification
from app.database.models.enums import RfqStatus
from app.services.rfq_service import rfq_service
from app.config.settings import settings
from app.core.logger import get_logger


logger = get_logger(__name__)


# ============================================================================
# Scheduler instance (module-level singleton)
# ============================================================================
scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)


# ============================================================================
# JOB 1: Close Expired RFQs
# ============================================================================
async def job_close_expired_rfqs(bot: Optional[Any] = None):
    """
    Find every RFQ that has passed its deadline and is still in
    'published' status, then close it via rfq_service.

    Idempotency: only RFQs with status='published' and deadline <= now
    are picked up. Once closed, they won't be selected again. The service
    itself re-verifies the status inside the transaction.

    Notifications: rfq_service.close_rfq_system() handles notifying the
    company managers (via notification_service) when `bot` is provided.
    """
    logger.info("Scheduler job starting | job=close_expired_rfqs")
    now = datetime.now(timezone.utc)

    try:
        # System-wide query — silent to avoid warning noise.
        async with get_session(silent=True) as session:
            stmt = select(Rfq).where(
                Rfq.status == RfqStatus.published,
                Rfq.deadline <= now,
            )
            expired_rfqs = (await session.execute(stmt)).scalars().all()

        if not expired_rfqs:
            logger.debug("Scheduler job | job=close_expired_rfqs | found=0")
            return

        logger.info(
            "Scheduler job | job=close_expired_rfqs | found=%s",
            len(expired_rfqs),
        )

        closed_count = 0
        for rfq in expired_rfqs:
            try:
                ok = await rfq_service.close_rfq_system(
                    rfq_id=rfq.id,
                    bot=bot,
                )
                if ok:
                    closed_count += 1
                    logger.info(
                        "Scheduler closed RFQ | rfq_id=%s",
                        rfq.id,
                    )
            except Exception:
                logger.exception(
                    "Scheduler failed to close RFQ | rfq_id=%s",
                    rfq.id,
                )

        logger.info(
            "Scheduler job done | job=close_expired_rfqs | closed=%s",
            closed_count,
        )

    except Exception:
        logger.exception(
            "Scheduler job failed | job=close_expired_rfqs"
        )


# ============================================================================
# JOB 2: Send Deadline Reminders
# ============================================================================
async def job_send_deadline_reminders(bot: Optional[Any] = None):
    """
    Find RFQs closing soon (within 1 hour) and send a reminder to the
    company managers.

    Idempotency: checks the notifications table for a previous
    'rfq_deadline_reminder' entry for the same rfq_id.

    TODO: notification_service.notify_rfq_reminder() is not yet implemented.
    This job is a placeholder. Until the service method exists, it logs
    the candidates it would notify.
    """
    logger.info("Scheduler job starting | job=send_deadline_reminders")
    now = datetime.now(timezone.utc)
    reminder_threshold = now + timedelta(hours=1)

    try:
        async with get_session(silent=True) as session:
            stmt = select(Rfq).where(
                Rfq.status == RfqStatus.published,
                Rfq.deadline > now,
                Rfq.deadline <= reminder_threshold,
            )
            upcoming_rfqs = (await session.execute(stmt)).scalars().all()

        if not upcoming_rfqs:
            logger.debug(
                "Scheduler job | job=send_deadline_reminders | found=0"
            )
            return

        candidates: List[int] = []
        for rfq in upcoming_rfqs:
            # Idempotency check
            async with get_session(silent=True) as session:
                stmt_check = select(func.count(Notification.id)).where(
                    Notification.kind == "rfq_deadline_reminder",
                    Notification.payload["rfq_id"].as_integer() == rfq.id,
                )
                count = (await session.execute(stmt_check)).scalar() or 0

                if count > 0:
                    continue

            candidates.append(rfq.id)

        if not candidates:
            logger.debug(
                "Scheduler job | job=send_deadline_reminders | "
                "candidates=0 (all already reminded)"
            )
            return

        # TODO: uncomment when notification_service.notify_rfq_reminder exists
        # for rfq_id in candidates:
        #     await notification_service.notify_rfq_reminder(
        #         bot=bot, rfq_id=rfq_id,
        #     )

        logger.warning(
            "Scheduler job | job=send_deadline_reminders | "
            "candidates=%s | NOTE: notify_rfq_reminder not implemented yet",
            len(candidates),
        )

    except Exception:
        logger.exception(
            "Scheduler job failed | job=send_deadline_reminders"
        )


# ============================================================================
# Scheduler Initialization / Shutdown
# ============================================================================
def setup_scheduler(bot: Optional[Any] = None):
    """
    Configure and start the scheduler.

    Args:
        bot: The Aiogram Bot instance. It is passed to jobs so they can
             send Telegram notifications. If None, notifications are
             skipped (state changes still occur).
    """
    if not settings.SCHEDULER_ENABLED:
        logger.info("Scheduler disabled by settings.")
        return

    # Job 1: Close expired RFQs
    scheduler.add_job(
        job_close_expired_rfqs,
        trigger=IntervalTrigger(
            seconds=settings.RFQ_DEADLINE_CHECK_INTERVAL_SECONDS
        ),
        id="close_expired_rfqs",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        args=[bot],
    )

    # Job 2: Deadline reminders (placeholder)
    scheduler.add_job(
        job_send_deadline_reminders,
        trigger=IntervalTrigger(minutes=5),
        id="send_deadline_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        args=[bot],
    )

    scheduler.start()
    logger.info(
        "Scheduler started | interval=%ss | reminders=5m | timezone=%s",
        settings.RFQ_DEADLINE_CHECK_INTERVAL_SECONDS,
        settings.TIMEZONE,
    )


def shutdown_scheduler():
    """Gracefully shut down the scheduler. Called from main.py on shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down.")
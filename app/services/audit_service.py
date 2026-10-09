"""
Audit Service - Read-only access to audit logs and activity history.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Query audit_log for activity history (read-only).
- Provide filtered views by entity, action, user, and date range.
- Log explicit business events that are NOT covered by DB triggers.
- Enforce company isolation on all queries.

Key Rules from Architecture Document:
- audit_log is Append-Only (written by audit_trigger() in PostgreSQL).
- This service does NOT modify or delete audit records.
- Secrets, passwords, and unnecessary sensitive data are NEVER logged.
- Commercial audit (audit_log) is separate from technical error logs.
- All queries respect company isolation (RLS + explicit company_id filter).

IMPROVEMENTS (this revision):
- log_event() accepts an optional session so callers can include the
  audit entry in their own transaction (atomic state change + audit).
- log_event() accepts company_id=None for platform-level events
  (approve/reject/suspend supplier, etc.).
- log_security_event() and get_price_change_history() pass silent=True
  to avoid warning noise on intentional unscoped queries.
- get_price_change_history() distinguishes supplier scope from company
  scope via the new `as_supplier` flag.
- _sanitize_data() now also sanitizes nested lists of dicts.
- Logging uses %s formatting for consistency.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy import select, and_, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.audit_log import AuditLog
from app.database.models.price_change_log import PriceChangeLog
from app.database.models.enums import AuditAction
from app.core.logger import get_logger


logger = get_logger(__name__)


class AuditService:
    """
    Service class for Audit operations (primarily read-only).
    """

    # ========================================================================
    # 1. QUERY AUDIT LOG (Read-only)
    # ========================================================================

    @staticmethod
    async def get_entity_history(
        company_id: int,
        entity_table: str,
        entity_id: int,
        limit: int = 50,
    ) -> List[AuditLog]:
        """
        Get the full audit history for a specific entity.
        Used to display the state transition history of a request, RFQ, etc.

        Args:
            company_id: Company scope (RLS isolation).
            entity_table: The table name (e.g., 'purchase_requests', 'rfqs').
            entity_id: The entity's primary key.
            limit: Maximum number of records to return.
        """
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(AuditLog)
                .where(
                    AuditLog.company_id == company_id,
                    AuditLog.entity_table == entity_table,
                    AuditLog.entity_id == entity_id,
                )
                .order_by(desc(AuditLog.occurred_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    async def get_company_activity(
        company_id: int,
        action: Optional[AuditAction] = None,
        entity_table: Optional[str] = None,
        actor_user_id: Optional[int] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        Get filtered activity log for a company.
        Used by managers/owners to review recent actions.

        Args:
            company_id: Company scope.
            action: Filter by action type (APPROVE, REJECT, AWARD, etc.).
            entity_table: Filter by entity type.
            actor_user_id: Filter by who performed the action.
            since/until: Date range filter.
            limit/offset: Pagination.
        """
        async with get_session(company_id=company_id) as session:
            # Build base query
            stmt = select(AuditLog).where(AuditLog.company_id == company_id)

            # Apply filters
            if action:
                stmt = stmt.where(AuditLog.action == action)
            if entity_table:
                stmt = stmt.where(AuditLog.entity_table == entity_table)
            if actor_user_id:
                stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
            if since:
                stmt = stmt.where(AuditLog.occurred_at >= since)
            if until:
                stmt = stmt.where(AuditLog.occurred_at <= until)

            # Count total
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = (await session.execute(count_stmt)).scalar() or 0

            # Apply pagination and ordering
            stmt = (
                stmt.order_by(desc(AuditLog.occurred_at))
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            logs = list(result.scalars().all())

            return {
                "logs": logs,
                "total": total,
                "has_more": (offset + limit) < total,
            }

    @staticmethod
    async def get_recent_decisions(
        company_id: int,
        days: int = 7,
        limit: int = 20,
    ) -> List[AuditLog]:
        """
        Get recent approval/rejection/award decisions.
        Useful for dashboard summaries.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session(company_id=company_id) as session:
            stmt = (
                select(AuditLog)
                .where(
                    AuditLog.company_id == company_id,
                    AuditLog.action.in_(
                        [
                            AuditAction.APPROVE,
                            AuditAction.REJECT,
                            AuditAction.AWARD,
                        ]
                    ),
                    AuditLog.occurred_at >= since,
                )
                .order_by(desc(AuditLog.occurred_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ========================================================================
    # 2. EXPLICIT AUDIT LOGGING (For events not covered by DB triggers)
    # ========================================================================

    @staticmethod
    async def log_event(
        company_id: Optional[int],
        entity_table: str,
        entity_id: int,
        action: AuditAction,
        actor_user_id: Optional[int] = None,
        actor_role: Optional[str] = None,
        reason: Optional[str] = None,
        old_data: Optional[Dict[str, Any]] = None,
        new_data: Optional[Dict[str, Any]] = None,
        telegram_update_id: Optional[int] = None,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        """
        Explicitly log a business event to audit_log.

        Use this for events that are NOT automatically captured by
        audit_trigger(), such as:
        - LATE_SUBMISSION events
        - Security events (unauthorized access attempts)
        - Business rule violations
        - Manual interventions
        - State transitions performed by the scheduler
        - Platform-level events (company_id=None)

        TRANSACTION SEMANTICS
        ---------------------
        - If `session` is provided, the audit entry is added to that
          session and flushed (not committed). The caller is responsible
          for committing. This ensures the state change and the audit
          entry are part of the SAME transaction.
        - If `session` is None, a new session is opened and committed
          immediately (standalone use from handlers).

        Args:
            company_id: Company scope, or None for platform-level events
                        (e.g., supplier approval by Super Admin).
            entity_table: Table name of the audited entity.
            entity_id: Primary key of the audited entity.
            action: Audit action (APPROVE, REJECT, etc.).
            actor_user_id: User performing the action (None for system).
            actor_role: Role of the actor at the time.
            reason: Human-readable reason (for rejections, cancellations).
            old_data: Snapshot before change (sanitized).
            new_data: Snapshot after change (sanitized).
            telegram_update_id: Source Telegram update ID.
            session: Optional caller-managed session (for batching).

        NOTE: This does NOT replace audit_trigger(). The trigger handles
        standard CRUD operations automatically. This method is for
        supplementary business events.

        SECURITY: Never log secrets, passwords, API keys, or unnecessary
        sensitive data in old_data/new_data.
        """
        sanitized_old = AuditService._sanitize_data(old_data)
        sanitized_new = AuditService._sanitize_data(new_data)

        async def _write(sess: AsyncSession) -> bool:
            audit_entry = AuditLog(
                entity_table=entity_table,
                entity_id=entity_id,
                action=action,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                company_id=company_id,
                old_data=sanitized_old,
                new_data=sanitized_new,
                reason=reason,
                telegram_update_id=telegram_update_id,
            )
            sess.add(audit_entry)
            await sess.flush()  # Ensure INSERT is emitted within the caller's TX
            return True

        if session is not None:
            # Caller-managed session: join the caller's transaction.
            result = await _write(session)
            logger.info(
                "AUDIT | %s on %s#%s by user %s (company=%s, shared-tx)",
                action.value,
                entity_table,
                entity_id,
                actor_user_id,
                company_id,
            )
            return result

        # Standalone: open a new session and commit.
        async with get_session(company_id=company_id) as new_session:
            result = await _write(new_session)
            await new_session.commit()
            logger.info(
                "AUDIT | %s on %s#%s by user %s (company=%s)",
                action.value,
                entity_table,
                entity_id,
                actor_user_id,
                company_id,
            )
            return result

    @staticmethod
    async def log_security_event(
        company_id: Optional[int],
        entity_table: str,
        entity_id: int,
        actor_user_id: Optional[int],
        description: str,
        telegram_update_id: Optional[int] = None,
    ) -> bool:
        """
        Log a security event (unauthorized access attempt, scope violation, etc.).
        These events are critical for monitoring and should be reviewed regularly.

        Uses silent=True because security events may not belong to a single
        tenant (e.g., cross-tenant access attempts).
        """
        async with get_session(silent=True) as session:
            audit_entry = AuditLog(
                entity_table=entity_table,
                entity_id=entity_id,
                action=AuditAction.STATUS_CHANGE,  # Generic action for security events
                actor_user_id=actor_user_id,
                actor_role="SECURITY_EVENT",
                company_id=company_id,
                reason=f"SECURITY: {description}",
                telegram_update_id=telegram_update_id,
            )

            session.add(audit_entry)
            await session.commit()

            logger.warning(
                "SECURITY EVENT | %s | entity=%s#%s | user=%s",
                description,
                entity_table,
                entity_id,
                actor_user_id,
                extra={"security": True},
            )
            return True

    # ========================================================================
    # 3. PRICE CHANGE HISTORY (Read-only, logged by DB trigger)
    # ========================================================================

    @staticmethod
    async def get_price_change_history(
        supplier_id: int,
        item_id: Optional[int] = None,
        company_id: Optional[int] = None,
        as_supplier: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Get price change history from price_change_log.
        This log is populated automatically by log_price_change() trigger.

        Access rules:
        - If `as_supplier=True`, opens a supplier-owner session
          (supplier sees their own price changes).
        - Else if `company_id` is provided, opens a company-scoped
          session (buyer sees their custom price changes).
        - Else, falls back to a silent unscoped session (for
          administrative/internal use).

        Args:
            supplier_id: Supplier whose price changes to list.
            item_id: Optional item filter.
            company_id: Optional company filter (for custom price changes).
            as_supplier: If True, use supplier-owner session (RLS scope).
            limit: Max number of records.
        """
        if as_supplier:
            ctx = get_session(is_supplier_owner=True)
        elif company_id is not None:
            ctx = get_session(company_id=company_id)
        else:
            ctx = get_session(silent=True)

        async with ctx as session:
            stmt = select(PriceChangeLog).where(
                PriceChangeLog.supplier_id == supplier_id
            )

            if item_id:
                stmt = stmt.where(PriceChangeLog.item_id == item_id)

            if company_id is not None:
                # Company users can only see their own custom price changes
                stmt = stmt.where(PriceChangeLog.company_id == company_id)

            stmt = stmt.order_by(desc(PriceChangeLog.changed_at)).limit(limit)
            result = await session.execute(stmt)
            logs = result.scalars().all()

            return [
                {
                    "id": log.id,
                    "price_rule_id": log.price_rule_id,
                    "item_id": log.item_id,
                    "company_id": log.company_id,
                    "price_type": log.price_type.value if log.price_type else None,
                    "action": log.action.value if log.action else None,
                    "old_data": log.old_data,
                    "new_data": log.new_data,
                    "changed_by": log.changed_by,
                    "changed_at": log.changed_at.isoformat()
                    if log.changed_at
                    else None,
                }
                for log in logs
            ]

    # ========================================================================
    # 4. STATISTICS & SUMMARIES
    # ========================================================================

    @staticmethod
    async def get_activity_summary(
        company_id: int,
        days: int = 30,
    ) -> Dict[str, Any]:
        """
        Get a summary of company activity for dashboard display.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session(company_id=company_id) as session:
            # Total actions
            total_stmt = select(func.count(AuditLog.id)).where(
                AuditLog.company_id == company_id,
                AuditLog.occurred_at >= since,
            )
            total_actions = (await session.execute(total_stmt)).scalar() or 0

            # Actions by type
            by_action_stmt = (
                select(AuditLog.action, func.count(AuditLog.id))
                .where(
                    AuditLog.company_id == company_id,
                    AuditLog.occurred_at >= since,
                )
                .group_by(AuditLog.action)
            )
            by_action_result = await session.execute(by_action_stmt)
            actions_by_type = {
                row[0].value: row[1] for row in by_action_result.all()
            }

            # Actions by entity type
            by_entity_stmt = (
                select(AuditLog.entity_table, func.count(AuditLog.id))
                .where(
                    AuditLog.company_id == company_id,
                    AuditLog.occurred_at >= since,
                )
                .group_by(AuditLog.entity_table)
            )
            by_entity_result = await session.execute(by_entity_stmt)
            actions_by_entity = {
                row[0]: row[1] for row in by_entity_result.all()
            }

            # Most active users
            by_user_stmt = (
                select(
                    AuditLog.actor_user_id,
                    func.count(AuditLog.id).label("action_count"),
                )
                .where(
                    AuditLog.company_id == company_id,
                    AuditLog.occurred_at >= since,
                    AuditLog.actor_user_id.is_not(None),
                )
                .group_by(AuditLog.actor_user_id)
                .order_by(desc("action_count"))
                .limit(5)
            )
            by_user_result = await session.execute(by_user_stmt)
            top_users = [
                {"user_id": row[0], "count": row[1]}
                for row in by_user_result.all()
            ]

            return {
                "period_days": days,
                "total_actions": total_actions,
                "actions_by_type": actions_by_type,
                "actions_by_entity": actions_by_entity,
                "top_active_users": top_users,
            }

    # ========================================================================
    # 5. HELPERS
    # ========================================================================

    @staticmethod
    def _sanitize_data(
        data: Optional[Any],
    ) -> Optional[Any]:
        """
        Remove potentially sensitive fields from data before logging.
        Ensures no secrets are stored in audit_log.

        Handles:
        - dicts (recursively)
        - lists (recursively for dict elements)
        - other values are returned as-is
        """
        if data is None:
            return None

        # Fields that should NEVER be logged
        sensitive_fields = {
            "password",
            "token",
            "secret",
            "api_key",
            "bot_token",
            "authorization",
            "credential",
            "session_id",
            "access_token",
            "refresh_token",
            "private_key",
            "telegram_token",
        }

        # Handle dicts
        if isinstance(data, dict):
            sanitized: Dict[str, Any] = {}
            for key, value in data.items():
                if isinstance(key, str) and key.lower() in sensitive_fields:
                    sanitized[key] = "[REDACTED]"
                else:
                    sanitized[key] = AuditService._sanitize_data(value)
            return sanitized

        # Handle lists
        if isinstance(data, list):
            return [AuditService._sanitize_data(item) for item in data]

        # Scalar values pass through
        return data

    @staticmethod
    def format_action_arabic(action: AuditAction) -> str:
        """Get Arabic display text for an audit action."""
        action_map = {
            AuditAction.INSERT: "➕ إنشاء",
            AuditAction.UPDATE: "✏️ تعديل",
            AuditAction.DELETE: "🗑️ حذف",
            AuditAction.APPROVE: "✅ اعتماد",
            AuditAction.REJECT: "❌ رفض",
            AuditAction.AWARD: "🏆 ترسية",
            AuditAction.STATUS_CHANGE: "🔄 تغيير حالة",
        }
        return action_map.get(action, str(action.value))

    @staticmethod
    def format_entity_arabic(entity_table: str) -> str:
        """Get Arabic display text for an entity table."""
        entity_map = {
            "purchase_requests": "طلب شراء",
            "rfqs": "طلب عروض أسعار",
            "quotations": "عرض سعر",
            "award_decisions": "قرار ترسية",
            "goods_receipts": "استلام بضائع",
            "companies": "شركة",
            "suppliers": "مورد",
            "users": "مستخدم",
            "price_rules": "قاعدة سعر",
            "supplier_relations": "علاقة شركة-مورد",
        }
        return entity_map.get(entity_table, entity_table)


# Singleton instance
audit_service = AuditService()
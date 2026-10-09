"""
Database connection manager with RLS context support.
Aligned with Procurement Bot Database Schema v2.0 (PostgreSQL 15+).

Key architecture decisions:
- Async engine (asyncpg) for Aiogram 3.x compatibility.
- RLS context injection via set_config() in every session.
- Context manager pattern for safe session lifecycle.
- SET LOCAL (is_local=true) ensures settings don't leak
  to other requests reusing the same pooled connection.
- SECURITY: echo is controlled by SQL_ECHO (never by DEBUG).
- OBSERVABILITY: Unscoped sessions (no company_id, no platform_admin)
  are logged as warnings so they can be audited later, UNLESS the
  caller explicitly passes silent=True to declare the operation as
  an intentional bootstrap lookup (e.g. middleware user resolution).
"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
)

from app.config.settings import settings
from app.core.logger import get_logger


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# URL Conversion
# ---------------------------------------------------------------------------

def _get_async_url(url: str) -> str:
    """
    Convert sync PostgreSQL URL to async (asyncpg) URL.
    Handles both 'postgresql://' and 'postgresql+psycopg2://' prefixes.
    """
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


# ---------------------------------------------------------------------------
# Engine & Session Factory
# ---------------------------------------------------------------------------

# SECURITY: echo is controlled by a dedicated SQL_ECHO flag, NOT DEBUG.
# Enabling DEBUG must never leak SQL statements (which may contain PII,
# prices, tenant IDs) into logs.
engine = create_async_engine(
    _get_async_url(settings.DATABASE_URL),
    echo=settings.SQL_ECHO,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# Session Context Manager with RLS
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_session(
    company_id: Optional[int] = None,
    is_supplier_owner: bool = False,
    is_platform_admin: bool = False,
    silent: bool = False,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Provides an async database session with RLS context injected.

    SCOPE CONTRACT
    --------------
    Callers should declare their scope when possible:

      - Tenant-scoped:     get_session(company_id=<id>)
      - Supplier owner:    get_session(company_id=<id>, is_supplier_owner=True)
      - Platform (admin):  get_session(is_platform_admin=True)
      - Bootstrap lookup:  get_session(silent=True)

    Unscoped calls (no company_id, no is_platform_admin) are allowed for
    legitimate cross-tenant operations (global lookup tables, user discovery
    before tenant is known, scheduler tasks).

    By default, unscoped calls are logged as warnings. Pass silent=True
    to suppress the warning when the caller is certain the operation is
    an intentional bootstrap lookup (e.g. middleware user resolution).

    Sets PostgreSQL session variables via set_config(..., true):
      - app.company_id        → used by RLS policies for tenant isolation
      - app.is_supplier_owner → allows supplier owners to see their own prices
      - app.is_platform_admin → marks platform-level operations (super admin)

    set_config(..., true) makes each setting LOCAL to the current
    transaction, preventing leakage to other requests that reuse the
    same pooled connection.

    Usage:
        # Tenant scope
        async with get_session(company_id=user.company_id) as session:
            ...

        # Platform scope (super admin)
        async with get_session(is_platform_admin=True) as session:
            ...

        # Global lookup (Role, UoM) — warning will be logged
        async with get_session() as session:
            ...

        # Bootstrap lookup (middleware user resolution) — no warning
        async with get_session(silent=True) as session:
            ...

    Args:
        company_id: Company ID for RLS isolation.
        is_supplier_owner: True if current user is a supplier owner.
        is_platform_admin: True for platform-level operations (super admin).
        silent: If True, suppress the warning for unscoped sessions.
                Use only for intentional bootstrap lookups.

    Yields:
        AsyncSession with RLS context active (or unscoped).

    Raises:
        Any exception from the database layer (after rollback).
    """
    # --- Observability: warn on unscoped sessions (unless silent) ---
    if company_id is None and not is_platform_admin and not silent:
        logger.warning(
            "Unscoped DB session opened (company_id=None, is_platform_admin=False). "
            "RLS will not isolate tenant data. If this is intentional "
            "(global table, user discovery, scheduler), pass silent=True "
            "to suppress this warning. Otherwise, pass company_id or "
            "is_platform_admin=True."
        )

    async with async_session_factory() as session:
        try:
            # --- Inject RLS context ---
            if company_id is not None:
                await session.execute(
                    text("SELECT set_config('app.company_id', :val, true)"),
                    {"val": str(company_id)},
                )

            if is_supplier_owner:
                await session.execute(
                    text("SELECT set_config('app.is_supplier_owner', 'on', true)")
                )

            if is_platform_admin:
                await session.execute(
                    text("SELECT set_config('app.is_platform_admin', 'on', true)")
                )

            yield session

            # Auto-commit on success
            await session.commit()

        except Exception as exc:
            await session.rollback()
            logger.exception(
                "DB session failed | company_id=%s | supplier_owner=%s | admin=%s | error=%s",
                company_id,
                is_supplier_owner,
                is_platform_admin,
                exc,
            )
            raise


# ---------------------------------------------------------------------------
# Lifecycle Helpers
# ---------------------------------------------------------------------------

async def dispose_engine() -> None:
    """Dispose the engine and close all pooled connections. Call on shutdown."""
    await engine.dispose()
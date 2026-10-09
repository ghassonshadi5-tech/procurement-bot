"""
User Service - Business logic for managing users and roles.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- User lifecycle (create, fetch, update status).
- Role assignment and revocation via user_roles table.
- Super admin seeding on startup.
- All DB operations are async and respect RLS context when applicable.

IMPROVEMENTS (this revision):
- Unscoped sessions use silent=True (bootstrap user lookups are
  intentional; they don't represent tenant-scoped operations).
- Uses get_logger(__name__) for correlation IDs and structured logs.
- Logging uses %s formatting.
- Note: explicit audit entries are NOT written for user_roles INSERT/DELETE
  or users UPDATE, because the PostgreSQL audit_trigger() captures them
  automatically (see Schema v2.0, section 5).
"""
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.user import User, UserStatus
from app.database.models.role import Role
from app.database.models.user_role import UserRole
from app.core.logger import get_logger


logger = get_logger(__name__)


class UserService:
    """
    Service class for User-related operations.
    """

    # ========================================================================
    # 1. READ
    # ========================================================================

    @staticmethod
    async def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
        """
        Retrieve a user by their Telegram ID.

        Uses silent=True because user lookup by Telegram ID is a
        bootstrap operation (the tenant is not yet known).
        """
        async with get_session(silent=True) as session:
            stmt = select(User).where(User.telegram_user_id == telegram_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    @staticmethod
    async def get_or_create_user(
        telegram_id: int,
        full_name: str,
        language: str = "ar",
    ) -> User:
        """
        Fetch an existing user or create a new one.

        New users are created with status='active' and no company/supplier
        affiliation. They will receive roles later via invitations or
        registration.

        Uses silent=True because this is a bootstrap operation (called
        before the tenant is known).
        """
        async with get_session(silent=True) as session:
            stmt = select(User).where(User.telegram_user_id == telegram_id)
            user = (await session.execute(stmt)).scalar_one_or_none()

            if user:
                # Optionally update full_name if changed in Telegram.
                if user.full_name != full_name:
                    user.full_name = full_name
                    await session.commit()
                return user

            # Create new user
            new_user = User(
                telegram_user_id=telegram_id,
                full_name=full_name,
                language=language,
                status=UserStatus.active,
            )
            session.add(new_user)
            await session.commit()
            await session.refresh(new_user)

            logger.info(
                "Created new user | user_id=%s | tg_id=%s",
                new_user.id,
                telegram_id,
            )
            return new_user

    @staticmethod
    async def list_company_users(company_id: int) -> List[User]:
        """
        Get all active users belonging to a specific company.

        Passes company_id to get_session() so RLS isolation is enforced.
        """
        async with get_session(company_id=company_id) as session:
            stmt = (
                select(User)
                .where(
                    User.company_id == company_id,
                    User.status == UserStatus.active,
                )
                .order_by(User.full_name.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ========================================================================
    # 2. ROLES
    # ========================================================================

    @staticmethod
    async def get_user_roles(user_id: int) -> List[Role]:
        """
        Get all roles assigned to a user.

        Uses silent=True: roles and user_roles are global tables, not
        tenant-scoped.
        """
        async with get_session(silent=True) as session:
            stmt = (
                select(Role)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    async def has_role(user_id: int, role_code: str) -> bool:
        """
        Check if a user has a specific role.

        Uses silent=True: user_roles and roles are global tables.
        """
        async with get_session(silent=True) as session:
            stmt = (
                select(UserRole)
                .join(Role, UserRole.role_id == Role.id)
                .where(
                    UserRole.user_id == user_id,
                    Role.code == role_code,
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    @staticmethod
    async def assign_role(
        user_id: int,
        role_code: str,
        granted_by: Optional[int] = None,
    ) -> bool:
        """
        Assign a role to a user.

        Returns True if assigned, False if already has it or role doesn't
        exist. The INSERT on user_roles is captured by audit_trigger().

        Uses silent=True: roles and user_roles are global tables.
        """
        async with get_session(silent=True) as session:
            # Find role
            stmt = select(Role).where(Role.code == role_code)
            role = (await session.execute(stmt)).scalar_one_or_none()

            if not role:
                logger.warning(
                    "Attempted to assign non-existent role | role_code=%s",
                    role_code,
                )
                return False

            # Check if already assigned
            stmt = select(UserRole).where(
                UserRole.user_id == user_id,
                UserRole.role_id == role.id,
            )
            if (await session.execute(stmt)).scalar_one_or_none():
                return False  # Already has role

            # Assign role
            user_role = UserRole(
                user_id=user_id,
                role_id=role.id,
                granted_by=granted_by,
            )
            session.add(user_role)
            await session.commit()

            logger.info(
                "Assigned role | user_id=%s | role_code=%s | granted_by=%s",
                user_id,
                role_code,
                granted_by,
            )
            return True

    @staticmethod
    async def revoke_role(user_id: int, role_code: str) -> bool:
        """
        Revoke a role from a user.

        The DELETE on user_roles is captured by audit_trigger().

        Uses silent=True: roles and user_roles are global tables.
        """
        async with get_session(silent=True) as session:
            stmt = (
                select(UserRole)
                .join(Role, UserRole.role_id == Role.id)
                .where(
                    UserRole.user_id == user_id,
                    Role.code == role_code,
                )
            )
            user_role = (await session.execute(stmt)).scalar_one_or_none()

            if not user_role:
                return False

            await session.delete(user_role)
            await session.commit()

            logger.info(
                "Revoked role | user_id=%s | role_code=%s",
                user_id,
                role_code,
            )
            return True

    # ========================================================================
    # 3. STATUS
    # ========================================================================

    @staticmethod
    async def update_status(user_id: int, status: UserStatus) -> bool:
        """
        Update user account status (active, blocked, invited).

        The UPDATE on users is captured by audit_trigger().

        Uses silent=True: user status changes are administrative and
        cross-tenant by nature (e.g., blocking a user).
        """
        async with get_session(silent=True) as session:
            stmt = select(User).where(User.id == user_id)
            user = (await session.execute(stmt)).scalar_one_or_none()

            if not user:
                return False

            user.status = status
            await session.commit()

            logger.info(
                "Updated user status | user_id=%s | status=%s",
                user_id,
                status.value,
            )
            return True

    # ========================================================================
    # 4. BOOTSTRAP
    # ========================================================================

    @staticmethod
    async def ensure_super_admin(telegram_id: int, full_name: str) -> None:
        """
        Ensure the configured super admin exists and has the super_admin
        role. Should be called on bot startup.

        Uses bootstrap-friendly helpers so no unscoped-session warnings
        are emitted on startup.
        """
        user = await UserService.get_or_create_user(telegram_id, full_name)
        has_role = await UserService.has_role(user.id, "super_admin")

        if not has_role:
            await UserService.assign_role(user.id, "super_admin")
            logger.info(
                "Ensured super_admin role | user_id=%s | tg_id=%s",
                user.id,
                telegram_id,
            )


# Singleton instance for easy import
user_service = UserService()
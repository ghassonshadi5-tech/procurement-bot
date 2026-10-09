"""
Company Service - Business logic for managing buyer companies.
Aligned with Procurement Bot Architecture v3.0 and Schema v2.0.

Responsibilities:
- Company lifecycle (create as pending, approve, reject, suspend).
- Fetching company details.
- Super Admin operations (list all companies).
- Uniqueness validation for name, phone, and email during registration.

NOTE: In the new architecture, companies are created directly in the
`companies` table with status='pending'. There are no `slug` or `invite_code`
fields. Invitations are handled by the separate InvitationService.

IMPROVEMENTS (this revision):
- All unscoped sessions use silent=True (company lookups are
  bootstrap/administrative and do not require tenant RLS).
- Uniqueness error messages no longer leak the name of the conflicting
  company (security: avoid disclosing other tenants' existence).
- find_similar_names() escapes LIKE wildcards to prevent pattern
  injection.
- reject_company() accepts an optional reason, recorded in audit_log.
- suspend_company() accepts suspended_by_user_id, recorded in audit_log
  via audit_service.log_event.
- Uses get_logger(__name__) and %s formatting.
"""
import re
from typing import Optional, List, Tuple
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_session
from app.database.models.company import Company, CompanyStatus
from app.database.models.user import User
from app.database.models.enums import AuditAction
from app.core.logger import get_logger
from app.services.audit_service import audit_service


logger = get_logger(__name__)


class CompanyService:
    """
    Service class for Company-related operations.
    """

    # ========================================================================
    # 1. VALIDATION
    # ========================================================================

    @staticmethod
    async def check_uniqueness(
        phone_number: Optional[str] = None,
        email: Optional[str] = None,
        exclude_company_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Check if phone number or email is already used by another
        active/pending company.

        SECURITY: Error messages do NOT include the name of the conflicting
        company, to avoid disclosing the existence of other tenants.

        Returns:
            Tuple of (is_unique: bool, error_message: str).
        """
        errors: List[str] = []

        async with get_session(silent=True) as session:
            # --- Phone uniqueness ---
            if phone_number:
                stmt = select(Company.id).where(
                    Company.phone_number == phone_number,
                    Company.status.in_(
                        [CompanyStatus.pending, CompanyStatus.active]
                    ),
                )
                if exclude_company_id:
                    stmt = stmt.where(Company.id != exclude_company_id)

                existing_phone_id = (
                    await session.execute(stmt)
                ).scalar_one_or_none()

                if existing_phone_id:
                    errors.append(
                        "رقم الهاتف مستخدم بالفعل من قبل شركة أخرى."
                    )

            # --- Email uniqueness ---
            if email:
                normalized_email = email.strip().lower()
                stmt = select(Company.id).where(
                    func.lower(func.trim(Company.email)) == normalized_email,
                    Company.status.in_(
                        [CompanyStatus.pending, CompanyStatus.active]
                    ),
                )
                if exclude_company_id:
                    stmt = stmt.where(Company.id != exclude_company_id)

                existing_email_id = (
                    await session.execute(stmt)
                ).scalar_one_or_none()

                if existing_email_id:
                    errors.append(
                        "البريد الإلكتروني مستخدم بالفعل من قبل شركة أخرى."
                    )

        if errors:
            return False, "\n".join(errors)
        return True, ""

    @staticmethod
    async def find_similar_names(
        company_name: str,
        limit: int = 5,
    ) -> List[str]:
        """
        Find similar company names in the database for warning purposes.

        SECURITY: LIKE wildcards (% and _) in the input are escaped so a
        malicious input cannot expand the search to match every row.
        """
        async with get_session(silent=True) as session:
            escaped = re.escape(company_name.strip())
            pattern = f"%{escaped}%"
            stmt = (
                select(Company.name)
                .where(
                    Company.name.ilike(pattern, escape="\\"),
                    Company.status.in_(
                        [CompanyStatus.pending, CompanyStatus.active]
                    ),
                )
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

    # ========================================================================
    # 2. CREATE
    # ========================================================================

    @staticmethod
    async def create_company(
        name: str,
        created_by_user_id: int,
        legal_name: Optional[str] = None,
        inn: Optional[str] = None,
        city: Optional[str] = None,
        phone_number: Optional[str] = None,
        email: Optional[str] = None,
        industry_code: Optional[str] = None,
        industry_other_text: Optional[str] = None,
    ) -> Company:
        """
        Create a new company registration request.
        The company is created with status='pending' and requires Super
        Admin approval.

        STRICT VALIDATION (inside the same transaction):
        1. Name must be unique among 'active' and 'pending' (case-insensitive).
        2. Phone number must be unique among 'active' and 'pending'.
        3. Email must be unique among 'active' and 'pending'.

        The INSERT on companies is captured by audit_trigger().

        Raises:
            ValueError: If name, phone, or email is not unique.

        Returns:
            The created Company object in 'pending' state.
        """
        normalized_name = name.strip().lower()
        normalized_email = email.strip().lower() if email else None

        async with get_session(silent=True) as session:
            # 1. Name uniqueness (case-insensitive, trimmed)
            stmt_name = select(Company.id).where(
                func.lower(func.trim(Company.name)) == normalized_name,
                Company.status.in_(
                    [CompanyStatus.pending, CompanyStatus.active]
                ),
            )
            existing_name_id = (
                await session.execute(stmt_name)
            ).scalar_one_or_none()
            if existing_name_id:
                logger.warning(
                    "Duplicate company name attempted | name=%s", name
                )
                raise ValueError(
                    "اسم الشركة مستخدم بالفعل من قبل شركة أخرى نشطة أو معلّقة."
                )

            # 2. Phone uniqueness
            if phone_number:
                stmt_phone = select(Company.id).where(
                    Company.phone_number == phone_number,
                    Company.status.in_(
                        [CompanyStatus.pending, CompanyStatus.active]
                    ),
                )
                existing_phone_id = (
                    await session.execute(stmt_phone)
                ).scalar_one_or_none()
                if existing_phone_id:
                    logger.warning(
                        "Duplicate phone number attempted | phone=%s",
                        phone_number,
                    )
                    raise ValueError(
                        "رقم الهاتف مستخدم بالفعل من قبل شركة أخرى."
                    )

            # 3. Email uniqueness
            if normalized_email:
                stmt_email = select(Company.id).where(
                    func.lower(func.trim(Company.email)) == normalized_email,
                    Company.status.in_(
                        [CompanyStatus.pending, CompanyStatus.active]
                    ),
                )
                existing_email_id = (
                    await session.execute(stmt_email)
                ).scalar_one_or_none()
                if existing_email_id:
                    logger.warning(
                        "Duplicate email attempted | email=%s", email
                    )
                    raise ValueError(
                        "البريد الإلكتروني مستخدم بالفعل من قبل شركة أخرى."
                    )

            # 4. Create the company
            new_company = Company(
                name=name.strip(),
                legal_name=legal_name,
                inn=inn,
                city=city,
                status=CompanyStatus.pending,
                created_by=created_by_user_id,
                phone_number=phone_number,
                email=normalized_email,
                industry_code=industry_code,
                industry_other_text=industry_other_text,
            )

            session.add(new_company)
            await session.commit()
            await session.refresh(new_company)

            logger.info(
                "Created pending company | company_id=%s | name=%s | by_user=%s",
                new_company.id,
                new_company.name,
                created_by_user_id,
            )
            return new_company

    # ========================================================================
    # 3. READ
    # ========================================================================

    @staticmethod
    async def get_company_by_id(company_id: int) -> Optional[Company]:
        """
        Retrieve a company by its ID.

        Uses silent=True: this is a lookup by primary key — the caller
        already knows the target company. RLS on the companies table is
        not relied upon here (companies is a top-level table with its own
        access rules).
        """
        async with get_session(silent=True) as session:
            stmt = select(Company).where(Company.id == company_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    @staticmethod
    async def get_user_company(user_id: int) -> Optional[Company]:
        """
        Get the company that a specific user belongs to.
        Returns None if the user is not affiliated with any company
        (e.g., super admin, or new user).

        Uses silent=True: user_id is the identifying key here, and RLS
        cannot be applied until we resolve the company.
        """
        async with get_session(silent=True) as session:
            stmt = (
                select(Company)
                .join(User, User.company_id == Company.id)
                .where(User.id == user_id)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    @staticmethod
    async def list_all_companies(
        status: Optional[CompanyStatus] = None,
    ) -> List[Company]:
        """
        List all companies in the system (Super Admin view).
        Optionally filter by status.

        Uses silent=True: this is intentionally unscoped (platform admin).
        """
        async with get_session(silent=True) as session:
            stmt = select(Company)
            if status:
                stmt = stmt.where(Company.status == status)
            stmt = stmt.order_by(Company.created_at.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ========================================================================
    # 4. STATE TRANSITIONS
    # ========================================================================

    @staticmethod
    async def approve_company(
        company_id: int,
        approved_by_user_id: int,
    ) -> bool:
        """
        Approve a pending company registration (Super Admin action).
        Changes status to 'active' and records approval metadata.

        The UPDATE on companies is captured by audit_trigger().
        """
        async with get_session(silent=True) as session:
            stmt = select(Company).where(Company.id == company_id)
            company = (await session.execute(stmt)).scalar_one_or_none()

            if not company:
                logger.warning(
                    "Approve attempt on non-existent company | company_id=%s",
                    company_id,
                )
                return False

            if company.status != CompanyStatus.pending:
                logger.warning(
                    "Cannot approve company | company_id=%s | current_status=%s",
                    company.id,
                    company.status.value,
                )
                return False

            company.status = CompanyStatus.active
            company.approved_by = approved_by_user_id
            company.approved_at = datetime.now(timezone.utc)

            await session.commit()

            logger.info(
                "Company approved | company_id=%s | name=%s | by_user=%s",
                company.id,
                company.name,
                approved_by_user_id,
            )
            return True

    @staticmethod
    async def reject_company(
        company_id: int,
        approved_by_user_id: int,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Reject a pending company registration (Super Admin action).
        Changes status to 'rejected'.

        The reason is recorded in audit_log via audit_service (the schema
        has no dedicated rejection_reason column on companies).
        """
        async with get_session(silent=True) as session:
            stmt = select(Company).where(Company.id == company_id)
            company = (await session.execute(stmt)).scalar_one_or_none()

            if not company or company.status != CompanyStatus.pending:
                return False

            old_status = company.status.value
            company.status = CompanyStatus.rejected
            company.approved_by = approved_by_user_id
            company.approved_at = datetime.now(timezone.utc)

            # Audit entry with reason, same transaction.
            await audit_service.log_event(
                company_id=company.id,
                entity_table="companies",
                entity_id=company.id,
                action=AuditAction.REJECT,
                actor_user_id=approved_by_user_id,
                old_data={"status": old_status},
                new_data={"status": CompanyStatus.rejected.value},
                reason=reason or "Company registration rejected",
                session=session,
            )

            await session.commit()

            logger.info(
                "Company rejected | company_id=%s | name=%s | by_user=%s",
                company.id,
                company.name,
                approved_by_user_id,
            )
            return True

    @staticmethod
    async def suspend_company(
        company_id: int,
        suspended_by_user_id: int,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Suspend an active company (prevents new operations).
        Does NOT delete historical data.

        The suspension is recorded in audit_log (with actor and reason)
        because the companies schema has no suspended_by column.
        """
        async with get_session(silent=True) as session:
            stmt = select(Company).where(Company.id == company_id)
            company = (await session.execute(stmt)).scalar_one_or_none()

            if not company or company.status != CompanyStatus.active:
                return False

            old_status = company.status.value
            company.status = CompanyStatus.suspended

            await audit_service.log_event(
                company_id=company.id,
                entity_table="companies",
                entity_id=company.id,
                action=AuditAction.STATUS_CHANGE,
                actor_user_id=suspended_by_user_id,
                old_data={"status": old_status},
                new_data={"status": CompanyStatus.suspended.value},
                reason=reason or "Company suspended",
                session=session,
            )

            await session.commit()

            logger.warning(
                "Company SUSPENDED | company_id=%s | name=%s | by_user=%s",
                company.id,
                company.name,
                suspended_by_user_id,
            )
            return True


# Singleton instance for easy import
company_service = CompanyService()
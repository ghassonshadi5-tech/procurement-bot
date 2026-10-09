"""
Application settings and configuration.
Aligned with Procurement Bot v3.0 Architecture (Schema v2.0).

Loads environment variables from .env file using pydantic-settings.

✅ SECURITY: Enforces required settings at startup (Fail Fast principle).
✅ SECURITY: No default DB password — must be explicit in .env.
✅ SECURITY: DEBUG cannot be True in production.
✅ MULTI-TENANCY: No default tenant — isolation enforced via RLS per request.
✅ ASYNC: DATABASE_URL is auto-converted to asyncpg in database.py.
✅ LOGGING: Configurable log directory, rotation, and backup count.
"""
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Resolve project root (one level above this file's directory)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Allowed values for constrained settings
_ALLOWED_APP_ENVS = {"development", "staging", "production"}
_ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    Uses pydantic-settings for validation and type conversion.
    """

    # =========================================================================
    # Telegram Bot Configuration
    # =========================================================================
    BOT_TOKEN: str = ""
    BOT_USERNAME: str = ""

    # Super Admin Telegram ID — the user who gets super_admin role on first run.
    # This is YOUR personal Telegram ID. Find it via @userinfobot on Telegram.
    SUPER_ADMIN_TELEGRAM_ID: Optional[int] = None

    # =========================================================================
    # Database Configuration (PostgreSQL 15+)
    # =========================================================================
    # Use plain postgresql:// — database.py auto-converts to asyncpg
    # SECURITY: No default value — must be set explicitly in .env
    DATABASE_URL: str

    # =========================================================================
    # Application Environment
    # =========================================================================
    APP_ENV: str = "development"  # development | staging | production
    DEBUG: bool = False  # SECURITY: default False; cannot be True in production
    LOG_LEVEL: str = "INFO"
    SQL_ECHO: bool = False  # SQLAlchemy echo; keep False in production

    # =========================================================================
    # Logging Configuration
    # =========================================================================
    # Directory where log files are written (app.log, errors.log, security.log)
    LOG_DIR: str = str(BASE_DIR / "logs")
    # Max size of a single log file before rotation (default: 10 MB)
    LOG_MAX_BYTES: int = 10 * 1024 * 1024
    # Number of rotated log files to keep (per file, per handler)
    LOG_BACKUP_COUNT: int = 10

    # =========================================================================
    # Localization (i18n)
    # =========================================================================
    DEFAULT_LANGUAGE: str = "ar"
    SUPPORTED_LANGUAGES: List[str] = ["ar", "ru", "en"]

    # =========================================================================
    # Timezone
    # =========================================================================
    # Server timezone used for deadline calculations and audit timestamps
    TIMEZONE: str = "UTC"

    # =========================================================================
    # Scheduler (APScheduler / Aiogram Scheduler)
    # =========================================================================
    SCHEDULER_ENABLED: bool = True
    # Check for RFQ deadlines every N seconds
    RFQ_DEADLINE_CHECK_INTERVAL_SECONDS: int = 60

    # =========================================================================
    # Attachments Storage
    # =========================================================================
    # Local filesystem path for attachments (future: can be replaced with S3)
    ATTACHMENTS_STORAGE_PATH: str = str(BASE_DIR / "storage" / "attachments")
    # Max upload size in bytes (default: 20 MB — Telegram limit)
    MAX_ATTACHMENT_SIZE_BYTES: int = 20 * 1024 * 1024

    # =========================================================================
    # RFQ Defaults
    # =========================================================================
    # Default RFQ deadline duration in hours (for open competitions)
    DEFAULT_RFQ_DEADLINE_HOURS: int = 6
    # Allowed RFQ deadline options (hours)
    RFQ_DEADLINE_OPTIONS_HOURS: List[int] = [2, 6, 24]

    # =========================================================================
    # Pydantic v2 Configuration
    # =========================================================================
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # =========================================================================
    # Validators (Fail Fast)
    # =========================================================================

    @field_validator("BOT_TOKEN")
    @classmethod
    def validate_bot_token(cls, v: str) -> str:
        if not v or v in ("", "your_telebot_bot_token_here", "YOUR_BOT_TOKEN"):
            raise ValueError(
                "BOT_TOKEN is not configured in .env file!\n"
                "Please add: BOT_TOKEN=your_actual_bot_token\n"
                "Get it from @BotFather on Telegram."
            )
        return v

    @field_validator("SUPER_ADMIN_TELEGRAM_ID")
    @classmethod
    def validate_super_admin_id(cls, v: Optional[int]) -> Optional[int]:
        if v is None or v == 0:
            raise ValueError(
                "SUPER_ADMIN_TELEGRAM_ID is not configured in .env file!\n"
                "Please add: SUPER_ADMIN_TELEGRAM_ID=your_actual_telegram_id\n"
                "Find your ID by messaging @userinfobot on Telegram.\n\n"
                "This user will automatically receive the 'super_admin' role "
                "on first bot startup."
            )
        return v

    @field_validator("SUPPORTED_LANGUAGES")
    @classmethod
    def validate_supported_languages(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("SUPPORTED_LANGUAGES cannot be empty")
        if "ar" not in v:
            v.append("ar")  # Arabic is mandatory as default
        return v

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "DATABASE_URL is not configured in .env file!\n"
                "Please add: DATABASE_URL=postgresql://user:pass@host:5432/dbname"
            )
        if "psycopg2" in v:
            # Auto-strip psycopg2 driver — database.py will convert to asyncpg
            v = v.replace("postgresql+psycopg2://", "postgresql://", 1)
        return v

    @field_validator("APP_ENV")
    @classmethod
    def validate_app_env(cls, v: str) -> str:
        if v not in _ALLOWED_APP_ENVS:
            raise ValueError(
                f"APP_ENV must be one of {sorted(_ALLOWED_APP_ENVS)}, got: {v!r}"
            )
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        v_upper = v.upper()
        if v_upper not in _ALLOWED_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(_ALLOWED_LOG_LEVELS)}, got: {v!r}"
            )
        return v_upper

    @field_validator("LOG_MAX_BYTES")
    @classmethod
    def validate_log_max_bytes(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(
                f"LOG_MAX_BYTES must be > 0, got: {v}"
            )
        # Sanity cap: 1 GB per file is more than enough
        if v > 1024 * 1024 * 1024:
            raise ValueError(
                f"LOG_MAX_BYTES seems too large (>1GB), got: {v}"
            )
        return v

    @field_validator("LOG_BACKUP_COUNT")
    @classmethod
    def validate_log_backup_count(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                f"LOG_BACKUP_COUNT must be >= 0, got: {v}"
            )
        return v

    @field_validator("DEFAULT_LANGUAGE")
    @classmethod
    def validate_default_language(cls, v: str, info) -> str:
        # Access already-validated SUPPORTED_LANGUAGES via info.data
        supported = info.data.get("SUPPORTED_LANGUAGES", [])
        if supported and v not in supported:
            raise ValueError(
                f"DEFAULT_LANGUAGE={v!r} is not in SUPPORTED_LANGUAGES={supported}"
            )
        return v

    @field_validator("TIMEZONE")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(
                f"TIMEZONE={v!r} is not a valid IANA timezone. "
                f"Examples: 'UTC', 'Europe/Moscow', 'Asia/Riyadh'. Error: {e}"
            )
        return v

    @field_validator("RFQ_DEADLINE_CHECK_INTERVAL_SECONDS")
    @classmethod
    def validate_rfq_deadline_interval(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(
                f"RFQ_DEADLINE_CHECK_INTERVAL_SECONDS must be > 0, got: {v}"
            )
        return v

    @field_validator("MAX_ATTACHMENT_SIZE_BYTES")
    @classmethod
    def validate_max_attachment_size(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(
                f"MAX_ATTACHMENT_SIZE_BYTES must be > 0, got: {v}"
            )
        # Telegram Bot API hard limit is 20 MB for downloads
        if v > 20 * 1024 * 1024:
            raise ValueError(
                f"MAX_ATTACHMENT_SIZE_BYTES cannot exceed Telegram's 20 MB limit, got: {v}"
            )
        return v

    @field_validator("DEFAULT_RFQ_DEADLINE_HOURS")
    @classmethod
    def validate_default_rfq_deadline(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(
                f"DEFAULT_RFQ_DEADLINE_HOURS must be > 0, got: {v}"
            )
        options = info.data.get("RFQ_DEADLINE_OPTIONS_HOURS", [])
        if options and v not in options:
            raise ValueError(
                f"DEFAULT_RFQ_DEADLINE_HOURS={v} must be one of "
                f"RFQ_DEADLINE_OPTIONS_HOURS={options}"
            )
        return v

    # =========================================================================
    # Cross-field Validators
    # =========================================================================

    @model_validator(mode="after")
    def validate_production_safety(self) -> "Settings":
        """
        SECURITY: Enforce production-safe defaults.
        - DEBUG must be False in production.
        - DB password must not be the placeholder 'password'.
        """
        if self.APP_ENV == "production":
            if self.DEBUG:
                raise ValueError(
                    "DEBUG=True is not allowed in production. "
                    "Set DEBUG=False in .env for APP_ENV=production."
                )
            # Reject the placeholder password pattern
            lowered = self.DATABASE_URL.lower()
            if "://postgres:password@" in lowered or ":password@" in lowered:
                raise ValueError(
                    "DATABASE_URL contains placeholder password 'password' "
                    "in production. Use a strong password in .env."
                )
        return self

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def is_super_admin(self, telegram_id: int) -> bool:
        """
        Check if a telegram_id belongs to the platform super admin.

        Args:
            telegram_id: The Telegram user ID to check.

        Returns:
            True if the user is the super admin, False otherwise.
        """
        return (
            self.SUPER_ADMIN_TELEGRAM_ID is not None
            and telegram_id == self.SUPER_ADMIN_TELEGRAM_ID
        )

    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.APP_ENV == "production"


# Global settings instance (singleton)
settings = Settings()
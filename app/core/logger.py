"""
Logging configuration for the application.

Provides a centralized, secure, and traceable logging setup:
- Console output (stdout) for development.
- Rotating file handlers for app, errors, and security events.
- Regex-based redaction of sensitive values (tokens, passwords, URLs).
- Correlation ID support via contextvars for end-to-end request tracing.
"""
import logging
import logging.handlers
import re
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from app.config.settings import settings


# =============================================================================
# Correlation ID (context-local, safe for async)
# =============================================================================
# Set by middleware at the start of each Telegram update.
# Read by the formatter on every log record.
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current async context."""
    _correlation_id.set(cid or "-")


def get_correlation_id() -> str:
    """Get the current correlation ID (or '-' if not set)."""
    return _correlation_id.get()


def clear_correlation_id() -> None:
    """Clear the correlation ID for the current async context."""
    _correlation_id.set("-")


# =============================================================================
# Security Filter (regex-based redaction)
# =============================================================================
class SecurityFilter(logging.Filter):
    """
    Redact sensitive values from log messages.

    Matches concrete patterns (not keywords) so that a leaked token is
    redacted even when its label is not present in the message.
    """

    # Each pattern: (compiled regex, replacement)
    _PATTERNS = [
        # Telegram Bot Token: 8-10 digits, colon, 35-char base64-ish
        (
            re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
            "<REDACTED:TELEGRAM_TOKEN>",
        ),
        # DB URL with credentials: scheme://user:pass@host
        (
            re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://[^:@/\s]+:[^@/\s]+@"),
            r"\g<scheme>://<REDACTED:CREDENTIALS>@",
        ),
        # Bearer tokens: Bearer <token>
        (
            re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{10,}\b"),
            "Bearer <REDACTED:BEARER>",
        ),
        # Authorization: <value> (when not Bearer)
        (
            re.compile(r"(?i)\bAuthorization\s*[:=]\s*[^\s,;\"']+"),
            "Authorization: <REDACTED>",
        ),
        # JSON-style password field: "password": "value"
        (
            re.compile(
                r'(?i)"(password|passwd|pwd|secret|token|api_key|apikey)"\s*:\s*"[^"]*"'
            ),
            r'"\1": "<REDACTED>"',
        ),
        # Key=value forms: password=..., token=..., secret=...
        (
            re.compile(
                r"(?i)\b(password|passwd|pwd|secret|token|api_key|apikey)\s*=\s*[^\s,;\"']+"
            ),
            r"\1=<REDACTED>",
        ),
        # AWS Access Key ID
        (
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            "<REDACTED:AWS_KEY>",
        ),
        # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_)
        (
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
            "<REDACTED:GITHUB_TOKEN>",
        ),
        # OpenAI-style keys: sk-...
        (
            re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
            "<REDACTED:API_KEY>",
        ),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact sensitive values in the rendered message; always allow."""
        try:
            rendered = record.getMessage()
        except Exception:
            # If rendering fails, let it pass to avoid breaking logging.
            return True

        redacted = rendered
        for pattern, replacement in self._PATTERNS:
            redacted = pattern.sub(replacement, redacted)

        if redacted != rendered:
            # Replace msg and drop args (already interpolated).
            record.msg = redacted
            record.args = None

        return True


# =============================================================================
# Correlation-ID Formatter
# =============================================================================
class CorrelationFormatter(logging.Formatter):
    """Formatter that injects the current correlation ID into each record."""

    def format(self, record: logging.LogRecord) -> str:
        record.correlation_id = get_correlation_id()
        return super().format(record)


# =============================================================================
# Logger Setup
# =============================================================================
_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | [cid=%(correlation_id)s] | %(name)s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Names already configured — prevents duplicate handlers on repeated imports.
_CONFIGURED_LOGGERS: set[str] = set()


def _ensure_log_dir() -> Path:
    """Create the log directory if it does not exist."""
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _build_rotating_handler(
    file_path: Path,
    level: int,
    formatter: logging.Formatter,
    security_filter: SecurityFilter,
) -> logging.Handler:
    """Build a RotatingFileHandler with common configuration."""
    handler = logging.handlers.RotatingFileHandler(
        filename=str(file_path),
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,  # Do not open the file until the first record is emitted.
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(security_filter)
    return handler


def setup_logger(
    name: str = "procurement_bot",
    level: Optional[str] = None,
) -> logging.Logger:
    """
    Configure and return a logger with console + file handlers.

    Safe to call multiple times with the same name: handlers are added once.

    Args:
        name: Logger name (use __name__ for module-specific loggers).
        level: Optional override for the configured LOG_LEVEL.

    Returns:
        Configured logger instance.
    """
    log_level_name = (level or settings.LOG_LEVEL).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    # Avoid duplicate handlers when called more than once.
    if name in _CONFIGURED_LOGGERS or logger.handlers:
        return logger

    formatter = CorrelationFormatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    security_filter = SecurityFilter()

    # --- Console handler (stdout) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(security_filter)
    logger.addHandler(console_handler)

    # --- File handlers (rotating) ---
    log_dir = _ensure_log_dir()

    # 1) app.log — everything at the configured level
    logger.addHandler(
        _build_rotating_handler(
            file_path=log_dir / "app.log",
            level=log_level,
            formatter=formatter,
            security_filter=security_filter,
        )
    )

    # 2) errors.log — WARNING and above
    logger.addHandler(
        _build_rotating_handler(
            file_path=log_dir / "errors.log",
            level=logging.WARNING,
            formatter=formatter,
            security_filter=security_filter,
        )
    )

    # 3) security.log — only records explicitly routed with extra={"security": True}
    #    plus WARNING+ from the SecurityFilter itself.
    security_handler = _build_rotating_handler(
        file_path=log_dir / "security.log",
        level=logging.INFO,
        formatter=formatter,
        security_filter=security_filter,
    )

    class _SecurityOnly(logging.Filter):
        """Only allow records explicitly tagged as security events."""

        def filter(self, record: logging.LogRecord) -> bool:
            return bool(getattr(record, "security", False))

    security_handler.addFilter(_SecurityOnly())
    logger.addHandler(security_handler)

    _CONFIGURED_LOGGERS.add(name)
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Return a module-specific logger.

    Usage:
        from app.core.logger import get_logger
        logger = get_logger(__name__)
    """
    return setup_logger(name)


# =============================================================================
# Global logger (backward compatibility)
# =============================================================================
# Existing modules import `logger` directly. Keep this as-is.
logger = setup_logger()
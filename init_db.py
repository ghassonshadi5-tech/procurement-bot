"""
Database Initialization Script.

Applies all Alembic migrations to bring the database schema up to date.

⚠️ FOR DEVELOPMENT AND DEPLOYMENT USE.

Usage:
    python init_db.py

What it does:
    1. Loads settings (validates DATABASE_URL and required env vars).
    2. Programmatically invokes Alembic to run "upgrade head".
    3. Reports success or failure with clear logging.

Notes:
    - Use this script after `reset_db.py` to recreate the schema.
    - For production, prefer running `alembic upgrade head` directly
      from the shell so that migrations can be reviewed in CI/CD logs.
"""
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config.settings import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

# Project root — location of alembic.ini
BASE_DIR = Path(__file__).resolve().parent
ALEMBIC_INI = BASE_DIR / "alembic.ini"


def run_migrations() -> None:
    """Apply all pending Alembic migrations (upgrade to head)."""
    if not ALEMBIC_INI.exists():
        logger.error("alembic.ini not found | path=%s", ALEMBIC_INI)
        sys.exit(1)

    logger.info("Initializing database | env=%s", settings.APP_ENV)

    alembic_cfg = Config(str(ALEMBIC_INI))

    try:
        command.upgrade(alembic_cfg, "head")
    except Exception:
        logger.exception("Failed to apply migrations")
        sys.exit(1)

    logger.info("Database schema is up to date. ✅")


if __name__ == "__main__":
    run_migrations()
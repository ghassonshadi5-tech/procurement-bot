"""
Reset Database Script - Drops and recreates the database.
Use this when init_db.py fails due to existing ENUMs or tables.

⚠️ FOR DEVELOPMENT ONLY - Never run this against a production database.

Usage:
    python reset_db.py

IMPROVEMENTS (this revision):
- SECURITY: validates the database name against a strict whitelist
  pattern before interpolating it into SQL. This prevents SQL injection
  if DATABASE_URL contains unusual characters.
- Uses get_logger(__name__) instead of print().
- Logs the DROP DATABASE action with extra={"security": True}.
"""
import asyncio
import re
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.settings import settings
from app.core.logger import get_logger

logger = get_logger(__name__)


# A conservative whitelist for PostgreSQL database names:
# - starts with a letter or underscore
# - contains only letters, digits, underscores
# - length 1..63 (PostgreSQL identifier limit)
_DB_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _extract_db_name(url: str) -> str:
    """
    Extract the database name from a PostgreSQL URL.

    Raises ValueError if the extracted name does not match the
    whitelist pattern.
    """
    parts = url.rsplit("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Malformed DATABASE_URL: cannot extract DB name.")

    db_name = parts[1].split("?", 1)[0]  # strip query params

    if not _DB_NAME_PATTERN.match(db_name):
        raise ValueError(
            f"Refusing to reset database: name {db_name!r} does not match "
            f"the allowed pattern {_DB_NAME_PATTERN.pattern}"
        )

    return db_name


async def reset_database():
    db_url = settings.DATABASE_URL
    if "asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # 1. Extract and validate the database name
    try:
        db_name = _extract_db_name(db_url)
    except ValueError as e:
        logger.error("Invalid database URL | error=%s", e)
        return

    # 2. Connect to the 'postgres' maintenance database (not the target)
    base_url = db_url.rsplit("/", 1)[0]
    postgres_url = f"{base_url}/postgres"

    engine = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")

    logger.warning(
        "RESET DATABASE requested | db_name=%s",
        db_name,
        extra={"security": True},
    )

    try:
        async with engine.connect() as conn:
            # 3. Terminate all active connections to the target DB
            logger.info(
                "Terminating active connections | db_name=%s", db_name
            )
            await conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :dbname
                      AND pid <> pg_backend_pid();
                    """
                ),
                {"dbname": db_name},
            )

            # 4. Drop the database (name already validated)
            logger.warning(
                "Dropping database | db_name=%s",
                db_name,
                extra={"security": True},
            )
            await conn.execute(
                text(f'DROP DATABASE IF EXISTS "{db_name}";')
            )

            # 5. Recreate the database
            logger.info("Creating fresh database | db_name=%s", db_name)
            await conn.execute(
                text(f'CREATE DATABASE "{db_name}";')
            )

        logger.info("Database reset successfully | db_name=%s", db_name)
        logger.info("Now run: python init_db.py")

    except Exception:
        logger.exception("Failed to reset database | db_name=%s", db_name)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(reset_database())
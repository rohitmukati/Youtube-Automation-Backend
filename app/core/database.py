import logging
import uuid
from importlib import import_module
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.base import Base

logger = logging.getLogger(__name__)


def build_engine() -> AsyncEngine:
    url = make_url(settings.database_url.get_secret_value())
    options = {"pool_pre_ping": True, "echo": False}
    if url.get_backend_name() == "postgresql":
        url = url.update_query_dict({"prepared_statement_cache_size": "0"})
        options.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            connect_args={
                "statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
            },
        )
    return create_async_engine(url, **options)


engine: AsyncEngine = build_engine()
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def create_tables() -> None:
    import_module("app.models")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("Database tables are ready")


async def check_database() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database health check failed")
        return False


async def dispose_engine() -> None:
    await engine.dispose()

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.database import check_database
from app.core.redis import check_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.app_env}


@router.get("/health/live")
async def liveness_check() -> dict:
    """Is the process running? Touches no database or Redis, so it never fails because of
    them. Use it for the container "is it alive" check: restarting the app would not fix
    a database outage, so this must not depend on one."""
    return {"status": "alive"}


async def _database_up() -> bool:
    try:
        return await asyncio.wait_for(check_database(), timeout=settings.health_timeout_seconds)
    except Exception:
        logger.warning("Database readiness check timed out or failed")
        return False


@router.get("/health/ready")
async def readiness_check() -> JSONResponse:
    """Can this instance serve traffic right now? Use it for the load balancer.

    The database is always required. Redis is required only when REDIS_URL is set,
    because then uploads and rate limits depend on it. Nothing secret is returned.
    """
    redis_required = settings.redis_configured
    database_ready, redis_ready = await asyncio.gather(
        _database_up(),
        check_redis() if redis_required else asyncio.sleep(0, result=True),
    )
    ready = database_ready and redis_ready
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "unavailable",
            "database": "up" if database_ready else "down",
            "redis": ("up" if redis_ready else "down") if redis_required else "not_configured",
        },
    )
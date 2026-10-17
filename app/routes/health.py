from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.database import check_database

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.app_env}


@router.get("/health/ready")
async def readiness_check() -> JSONResponse:
    database_ready = await check_database()
    status_code = 200 if database_ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if database_ready else "unavailable",
            "database": "up" if database_ready else "down",
        },
    )

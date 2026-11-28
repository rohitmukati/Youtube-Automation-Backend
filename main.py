# main.py
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.database import create_tables, dispose_engine
from app.core.handlers import register_exception_handlers
from app.core.logging import setup_logging
from app.core.observability import RequestIdMiddleware, init_sentry
from app.core.redis import close_redis
from app.routes import api_router
from app.routes import health
from app.workers.yt_channel_management import start_worker, stop_worker

setup_logging()
init_sentry()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    if settings.db_auto_create_tables:
        await create_tables()
    if settings.is_production and not settings.redis_configured:
        logger.warning(
            "REDIS_URL is not set in production: rate limits are per instance only"
        )
    worker = start_worker() if settings.yt_upload_worker_enabled else None
    logger.info("%s started in %s mode", settings.app_name, settings.app_env)
    yield
    if worker is not None:
        await stop_worker(worker)
    await close_redis()
    await dispose_engine()
    logger.info("%s stopped", settings.app_name)


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", settings.request_id_header],
    )
    # Added after CORS so it is the outermost layer: every response, including
    # CORS preflights and errors, carries a request ID.
    application.add_middleware(RequestIdMiddleware)

    register_exception_handlers(application)
    application.include_router(health.router)
    application.include_router(api_router, prefix=settings.api_v1_prefix)
    return application


app = create_app()
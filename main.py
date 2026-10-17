import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.database import create_tables, dispose_engine
from app.core.handlers import register_exception_handlers
from app.core.logging import setup_logging
from app.routes import api_router
from app.routes import health

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    if settings.db_auto_create_tables:
        await create_tables()
    logger.info("%s started in %s mode", settings.app_name, settings.app_env)
    yield
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
    )

    register_exception_handlers(application)
    application.include_router(health.router)
    application.include_router(api_router, prefix=settings.api_v1_prefix)
    return application


app = create_app()

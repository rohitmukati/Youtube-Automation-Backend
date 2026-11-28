# app/core/logging.py
import logging
import logging.config

from app.config import settings


def setup_logging() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s | %(levelname)s | %(request_id)s | %(name)s | %(message)s",
                }
            },
            "filters": {
                "request_id": {"()": "app.core.observability.RequestIdFilter"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout",
                    "filters": ["request_id"],
                }
            },
            "root": {"level": settings.log_level, "handlers": ["console"]},
            "loggers": {
                "sqlalchemy.engine": {
                    "level": "INFO" if settings.log_sql else "WARNING",
                    "handlers": ["console"],
                    "propagate": False,
                },
            },
        }
    )
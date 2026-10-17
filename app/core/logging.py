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
                    "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"level": settings.log_level, "handlers": ["console"]},
            "loggers": {
                "sqlalchemy.engine": {
                    "level": "INFO" if settings.debug else "WARNING",
                    "handlers": ["console"],
                    "propagate": False,
                },
            },
        }
    )

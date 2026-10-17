import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException, ServiceUnavailableError

logger = logging.getLogger(__name__)

VALIDATION_MESSAGE_PREFIX = "Value error, "


def build_error_response(
    status_code: int,
    code: str,
    message: str,
    details: Optional[List[Dict[str, Any]]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    error: Dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "error": error},
        headers=headers,
    )


def format_validation_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    formatted = []
    for error in errors:
        location = [str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")]
        message = str(error.get("msg", "Invalid value."))
        if message.startswith(VALIDATION_MESSAGE_PREFIX):
            message = message[len(VALIDATION_MESSAGE_PREFIX):]
        elif error.get("type") == "missing":
            message = "This field is required."
        formatted.append({"field": ".".join(location), "message": message})
    return formatted


async def handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error("Application error on %s %s: %s", request.method, request.url.path, exc.message)
    return build_error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = format_validation_errors(exc.errors())
    return build_error_response(
        422,
        "validation_error",
        "Some of the information you entered is not valid. Please review it and try again.",
        details,
    )


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
    return build_error_response(exc.status_code, "http_error", message, headers=getattr(exc, "headers", None))


async def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return build_error_response(
        ServiceUnavailableError.status_code,
        ServiceUnavailableError.code,
        ServiceUnavailableError.message,
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return build_error_response(
        500,
        "internal_error",
        "Something went wrong on our side. Please try again later.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppException, handle_app_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(SQLAlchemyError, handle_database_error)
    app.add_exception_handler(Exception, handle_unexpected_error)

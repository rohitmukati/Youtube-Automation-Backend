# app/core/observability.py
"""Request IDs in every log line, and optional Sentry.

A request ID lets you follow one user action across the API logs and, later, the
background workers. The client may send its own ID (a load balancer usually does); we
accept it only when it looks safe, so nobody can inject odd text into the logs.
"""
import contextvars
import logging
import re
import uuid
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def new_request_id(incoming: Optional[str]) -> str:
    if incoming and _SAFE_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIdMiddleware:
    """Pure ASGI middleware (does not buffer or wrap streaming responses)."""

    def __init__(self, app) -> None:
        self.app = app
        self._header = settings.request_id_header.lower().encode("latin-1")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == self._header:
                incoming = value.decode("latin-1")
                break
        request_id = new_request_id(incoming)
        # Also kept on the scope: the handler for unexpected 500 errors runs OUTSIDE this
        # middleware (Starlette builds that response in its outermost layer), so it reads the
        # ID from the request instead of from the context variable.
        scope["request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_with_id(message) -> None:
            if message["type"] == "http.response.start":
                headers = [(n, v) for n, v in message.get("headers", []) if n.lower() != self._header]
                headers.append((self._header, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def init_sentry() -> bool:
    """Start Sentry when SENTRY_DSN is set. Never stops the app from starting."""
    if not settings.sentry_dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return False
    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            send_default_pii=False,
        )
    except Exception:
        logger.exception("Sentry could not be started")
        return False
    logger.info("Sentry is on")
    return True
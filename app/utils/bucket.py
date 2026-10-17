import asyncio
import logging
import threading
from typing import BinaryIO, Optional
from urllib.parse import quote

import requests

from app.config import settings
from app.core.exceptions import ServiceUnavailableError, ValidationError

logger = logging.getLogger(__name__)

_bucket_lock = threading.Lock()
_bucket_ready = False

_NOT_CONFIGURED = "File storage is not configured yet. Please contact support."
_UNREACHABLE = "File storage is not reachable right now. Please try again in a moment."
_REQUEST_FAILED = "File storage could not complete the request. Please try again."


def _storage_base_url() -> str:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise ServiceUnavailableError(_NOT_CONFIGURED)
    return f"{settings.supabase_url.rstrip('/')}/storage/v1"


def _auth_headers() -> dict[str, str]:
    key = settings.supabase_service_role_key.get_secret_value()
    headers = {"apikey": key}
    if key.count(".") == 2:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _quoted_path(path: str) -> str:
    return quote(path, safe="/")


def _object_url(path: str) -> str:
    return (
        f"{_storage_base_url()}/object/"
        f"{quote(settings.supabase_storage_bucket, safe='')}/{_quoted_path(path)}"
    )


def _sign_url(path: Optional[str] = None) -> str:
    base = f"{_storage_base_url()}/object/sign/{quote(settings.supabase_storage_bucket, safe='')}"
    return f"{base}/{_quoted_path(path)}" if path else base


def _send(
    method: str,
    url: str,
    *,
    timeout: Optional[int] = None,
    headers: Optional[dict[str, str]] = None,
    **kwargs,
) -> requests.Response:
    merged_headers = {**_auth_headers(), **(headers or {})}
    try:
        return requests.request(
            method,
            url,
            headers=merged_headers,
            timeout=timeout or settings.storage_request_timeout_seconds,
            **kwargs,
        )
    except requests.RequestException:
        logger.exception("Storage request failed: %s %s", method, url)
        raise ServiceUnavailableError(_UNREACHABLE)


def _fail(response: requests.Response, action: str) -> None:
    logger.error(
        "Storage %s failed with status %s: %s",
        action,
        response.status_code,
        response.text[:300],
    )
    raise ServiceUnavailableError(_REQUEST_FAILED)


def _absolute_signed_url(signed: str) -> str:
    if signed.startswith(("http://", "https://")):
        return signed
    base = _storage_base_url()
    if signed.startswith("/storage/v1"):
        return f"{base[: -len('/storage/v1')]}{signed}"
    return f"{base}/{signed.lstrip('/')}"


def _ensure_bucket_sync() -> None:
    global _bucket_ready
    if _bucket_ready:
        return
    with _bucket_lock:
        if _bucket_ready:
            return
        bucket_id = settings.supabase_storage_bucket
        base = _storage_base_url()
        response = _send("GET", f"{base}/bucket/{quote(bucket_id, safe='')}")
        if response.status_code == 200:
            _bucket_ready = True
            return
        if response.status_code not in (400, 404):
            _fail(response, "bucket lookup")
        created = _send(
            "POST",
            f"{base}/bucket",
            json={"id": bucket_id, "name": bucket_id, "public": False},
        )
        if created.status_code in (200, 201, 409):
            _bucket_ready = True
            return
        _fail(created, "bucket creation")


def _upload_sync(path: str, fileobj: BinaryIO, content_type: str, size: int) -> None:
    fileobj.seek(0)
    response = _send(
        "POST",
        _object_url(path),
        timeout=settings.storage_upload_timeout_seconds,
        data=fileobj,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(size),
            "x-upsert": "false",
        },
    )
    if response.status_code in (200, 201):
        return
    if response.status_code == 413:
        raise ValidationError("This file is larger than the storage limit allows.")
    _fail(response, "upload")


def _delete_sync(path: str) -> None:
    response = _send("DELETE", _object_url(path))
    if response.status_code in (200, 204, 404):
        return
    _fail(response, "delete")


def _create_signed_url_sync(path: str, expires_in: int) -> str:
    response = _send("POST", _sign_url(path), json={"expiresIn": expires_in})
    if response.status_code != 200:
        _fail(response, "signed url")
    signed = response.json().get("signedURL")
    if not signed:
        _fail(response, "signed url")
    return _absolute_signed_url(signed)


def _create_signed_urls_sync(paths: list[str], expires_in: int) -> dict[str, str]:
    if not paths:
        return {}
    response = _send("POST", _sign_url(), json={"expiresIn": expires_in, "paths": paths})
    if response.status_code != 200:
        _fail(response, "signed urls")
    urls: dict[str, str] = {}
    for item in response.json():
        path = item.get("path")
        signed = item.get("signedURL")
        if path and signed and not item.get("error"):
            urls[path] = _absolute_signed_url(signed)
    return urls


async def ensure_bucket() -> None:
    await asyncio.to_thread(_ensure_bucket_sync)


async def upload_object(path: str, fileobj: BinaryIO, content_type: str, size: int) -> None:
    await asyncio.to_thread(_upload_sync, path, fileobj, content_type, size)


async def delete_object(path: str) -> None:
    await asyncio.to_thread(_delete_sync, path)


async def create_signed_url(path: str, expires_in: Optional[int] = None) -> str:
    ttl = expires_in or settings.storage_signed_url_ttl_seconds
    return await asyncio.to_thread(_create_signed_url_sync, path, ttl)


async def create_signed_urls(
    paths: list[str], expires_in: Optional[int] = None
) -> dict[str, str]:
    ttl = expires_in or settings.storage_signed_url_ttl_seconds
    return await asyncio.to_thread(_create_signed_urls_sync, paths, ttl)
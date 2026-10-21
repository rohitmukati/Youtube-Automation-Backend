# app/utils/bucket.py
import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from typing import BinaryIO, Optional
from urllib.parse import urlparse

import boto3
from boto3.exceptions import S3UploadFailedError
from boto3.s3.transfer import TransferConfig
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    HTTPClientError,
    IncompleteReadError,
)
from botocore.exceptions import ConnectionError as BotoConnectionError
from s3transfer.exceptions import RetriesExceededError

from app.config import settings
from app.core.exceptions import (
    AppException,
    ServiceUnavailableError,
    ValidationError,
)

logger = logging.getLogger(__name__)

_NOT_CONFIGURED = "File storage is not configured yet. Please contact support."
_UNREACHABLE = "File storage is not reachable right now. Please try again in a moment."
_REQUEST_FAILED = "File storage could not complete the request. Please try again."
_TOO_LARGE = "This file is larger than the storage limit allows."

_CONFIG_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccountProblem",
        "InvalidAccessKeyId",
        "InvalidBucketName",
        "InvalidToken",
        "ExpiredToken",
        "NoSuchBucket",
        "NotAuthorized",
        "SignatureDoesNotMatch",
        "Unauthorized",
    }
)
_TEMPORARY_ERROR_CODES = frozenset(
    {
        "InternalError",
        "RequestTimeout",
        "RequestTimeTooSkewed",
        "ServiceUnavailable",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
    }
)
_CONNECTION_ERRORS = (BotoConnectionError, HTTPClientError, IncompleteReadError)
_STORAGE_ERRORS = (
    BotoCoreError,
    ClientError,
    S3UploadFailedError,
    RetriesExceededError,
    OSError,
)

_client_lock = threading.Lock()
_clients: dict[tuple, BaseClient] = {}
_bucket_lock = threading.Lock()
_verified_buckets: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class _Target:
    endpoint: str
    bucket: str
    key_id: str
    secret: str


@dataclass(frozen=True)
class _Handle:
    client: BaseClient
    bucket: str


def _target() -> _Target:
    endpoint = (settings.b2_endpoint_url or "").strip().rstrip("/")
    bucket = (settings.b2_bucket_name or "").strip()
    key_id = (settings.b2_key_id or "").strip()
    secret = (
        settings.b2_application_key.get_secret_value().strip()
        if settings.b2_application_key is not None
        else ""
    )
    if not (endpoint and bucket and key_id and secret):
        raise ServiceUnavailableError(_NOT_CONFIGURED)
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.error("B2_ENDPOINT_URL is not a valid http or https URL.")
        raise ServiceUnavailableError(_NOT_CONFIGURED)
    return _Target(endpoint=endpoint, bucket=bucket, key_id=key_id, secret=secret)


def _region_for(endpoint: str) -> str:
    host = urlparse(endpoint).hostname or ""
    match = re.match(settings.storage_b2_endpoint_pattern, host)
    return match.group(1) if match else settings.storage_fallback_region


def _build_client(target: _Target, kind: str) -> BaseClient:
    read_timeout = (
        settings.storage_upload_timeout_seconds
        if kind == "upload"
        else settings.storage_request_timeout_seconds
    )
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        retries={"max_attempts": settings.storage_max_attempts, "mode": "standard"},
        connect_timeout=settings.storage_connect_timeout_seconds,
        read_timeout=read_timeout,
        max_pool_connections=settings.storage_max_pool_connections,
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        endpoint_url=target.endpoint,
        region_name=_region_for(target.endpoint),
        aws_access_key_id=target.key_id,
        aws_secret_access_key=target.secret,
        config=config,
    )


def _open(kind: str = "default") -> _Handle:
    target = _target()
    cache_key = (kind, target.endpoint, target.key_id, target.secret)
    client = _clients.get(cache_key)
    if client is None:
        with _client_lock:
            client = _clients.get(cache_key)
            if client is None:
                client = _build_client(target, kind)
                _clients[cache_key] = client
    return _Handle(client=client, bucket=target.bucket)


def reset_storage_cache() -> None:
    with _client_lock:
        _clients.clear()
    with _bucket_lock:
        _verified_buckets.clear()


def _error_details(exc: BaseException) -> tuple[str, int]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "", 0
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0
    try:
        return code, int(status)
    except (TypeError, ValueError):
        return code, 0


def _translate(exc: BaseException, action: str) -> AppException:
    if isinstance(exc, RetriesExceededError) and exc.last_exception is not None:
        return _translate(exc.last_exception, action)

    if isinstance(exc, S3UploadFailedError) and "EntityTooLarge" in str(exc):
        return ValidationError(_TOO_LARGE)

    if isinstance(exc, ClientError):
        code, status = _error_details(exc)
        if code == "EntityTooLarge" or status == 413:
            return ValidationError(_TOO_LARGE)
        if code in _CONFIG_ERROR_CODES or status in (401, 403):
            logger.error(
                "Storage %s was rejected (code=%s, status=%s). Check the B2 key, "
                "its bucket access and the Caps & Alerts page.",
                action,
                code or "unknown",
                status,
            )
            return ServiceUnavailableError(_NOT_CONFIGURED)
        if status == 429 or status >= 500 or code in _TEMPORARY_ERROR_CODES:
            logger.warning(
                "Storage %s failed temporarily (code=%s, status=%s).",
                action,
                code or "unknown",
                status,
            )
            return ServiceUnavailableError(_UNREACHABLE)
        logger.error(
            "Storage %s failed (code=%s, status=%s).", action, code or "unknown", status
        )
        return ServiceUnavailableError(_REQUEST_FAILED)

    if isinstance(exc, _CONNECTION_ERRORS):
        logger.warning("Storage %s could not reach the server: %s", action, type(exc).__name__)
        return ServiceUnavailableError(_UNREACHABLE)

    logger.error("Storage %s failed unexpectedly.", action, exc_info=exc)
    return ServiceUnavailableError(_REQUEST_FAILED)


def _ensure_bucket_sync() -> None:
    target = _target()
    marker = (target.endpoint, target.bucket)
    if marker in _verified_buckets:
        return
    with _bucket_lock:
        if marker in _verified_buckets:
            return
        handle = _open()
        try:
            handle.client.head_bucket(Bucket=handle.bucket)
        except ClientError as exc:
            code, status = _error_details(exc)
            if status == 404 or code in ("404", "NoSuchBucket"):
                logger.error("The B2 bucket %s does not exist or is not visible to the key.", handle.bucket)
                raise ServiceUnavailableError(_NOT_CONFIGURED) from exc
            try:
                handle.client.list_objects_v2(Bucket=handle.bucket, MaxKeys=1)
            except _STORAGE_ERRORS as inner:
                raise _translate(inner, "bucket check") from inner
        except _STORAGE_ERRORS as exc:
            raise _translate(exc, "bucket check") from exc
        _verified_buckets.add(marker)


def _list_version_ids(handle: _Handle, path: str) -> list[str]:
    response = handle.client.list_object_versions(
        Bucket=handle.bucket, Prefix=path, MaxKeys=1000
    )
    items = list(response.get("Versions") or []) + list(response.get("DeleteMarkers") or [])
    return [
        str(item["VersionId"])
        for item in items
        if item.get("Key") == path and item.get("VersionId")
    ]


def _delete_sync(path: str) -> None:
    handle = _open()
    try:
        version_ids: Optional[list[str]] = _list_version_ids(handle, path)
    except _STORAGE_ERRORS as exc:
        logger.warning(
            "Could not list stored versions during delete, using a plain delete: %s",
            type(exc).__name__,
        )
        version_ids = None

    try:
        if version_ids:
            for version_id in version_ids:
                try:
                    handle.client.delete_object(
                        Bucket=handle.bucket, Key=path, VersionId=version_id
                    )
                except ClientError as exc:
                    _, status = _error_details(exc)
                    if status != 404:
                        raise
        else:
            handle.client.delete_object(Bucket=handle.bucket, Key=path)
    except _STORAGE_ERRORS as exc:
        raise _translate(exc, "delete") from exc


def _delete_after_failure(path: str) -> None:
    try:
        _delete_sync(path)
    except AppException:
        logger.warning("Could not remove a partly stored object after a failed upload.")


def _verify_upload(handle: _Handle, path: str, size: int) -> None:
    try:
        head = handle.client.head_object(Bucket=handle.bucket, Key=path)
    except _STORAGE_ERRORS as exc:
        _delete_after_failure(path)
        raise _translate(exc, "upload check") from exc
    stored = head.get("ContentLength")
    if stored != size:
        logger.error("Stored object size %s does not match the uploaded size %s.", stored, size)
        _delete_after_failure(path)
        raise ServiceUnavailableError(_REQUEST_FAILED)


def _upload_sync(path: str, fileobj: BinaryIO, content_type: str, size: int) -> None:
    handle = _open("upload")
    fileobj.seek(0)
    transfer = TransferConfig(
        multipart_threshold=settings.storage_multipart_chunk_bytes,
        multipart_chunksize=settings.storage_multipart_chunk_bytes,
        max_concurrency=settings.storage_multipart_concurrency,
        use_threads=True,
    )
    transfer.max_in_memory_upload_chunks = settings.storage_multipart_buffered_chunks
    try:
        handle.client.upload_fileobj(
            fileobj,
            handle.bucket,
            path,
            ExtraArgs={"ContentType": content_type},
            Config=transfer,
        )
    except _STORAGE_ERRORS as exc:
        raise _translate(exc, "upload") from exc
    _verify_upload(handle, path, size)


def _presign(handle: _Handle, path: str, expires_in: int) -> str:
    ttl = max(1, min(int(expires_in), settings.storage_signed_url_max_ttl_seconds))
    try:
        return handle.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": handle.bucket, "Key": path},
            ExpiresIn=ttl,
            HttpMethod="GET",
        )
    except _STORAGE_ERRORS as exc:
        raise _translate(exc, "signed url") from exc


def _create_signed_url_sync(path: str, expires_in: int) -> str:
    return _presign(_open(), path, expires_in)


def _create_signed_urls_sync(paths: list[str], expires_in: int) -> dict[str, str]:
    if not paths:
        return {}
    handle = _open()
    return {path: _presign(handle, path, expires_in) for path in paths}


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
# app/integrations/youtube_api.py
import io
import json
import logging
import re
import time
from typing import Callable, Optional

import requests
from urllib3 import exceptions as urllib3_exceptions
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from app.config import settings
from app.utils.yt_channel_management import UploadFailure, UploadFailureError

logger = logging.getLogger(__name__)

_SLEEP_SLICE_SECONDS = 1.0
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_STORAGE_CODE = re.compile(r"<Code>([^<]{1,64})</Code>")
_sleep = time.sleep


class SessionExpiredError(Exception):
    pass


class _Transient(Exception):
    def __init__(
        self, reason: str, *, youtube: bool = False, reached_server: bool = True
    ) -> None:
        super().__init__(reason)
        self.youtube = youtube
        self.reached_server = reached_server


def _never_connected(exc: BaseException) -> bool:
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    inner = exc.args[0] if exc.args else None
    reason = getattr(inner, "reason", None)
    return isinstance(
        reason,
        (urllib3_exceptions.NewConnectionError, urllib3_exceptions.ConnectTimeoutError),
    )


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status


class YouTubeHttpError(Exception):

    def __init__(self, status: int, content: bytes) -> None:
        super().__init__(f"YouTube answered with status {status}")
        self.resp = _Resp(status)
        self.content = content


def _source_error(code: str, message: str, *, retryable: bool) -> UploadFailureError:
    return UploadFailureError(UploadFailure(code=code, message=message, retryable=retryable))


def _backoff(attempt: int, check_stop: Callable[[], None]) -> None:
    delay = min(
        settings.yt_upload_chunk_backoff_seconds * (2 ** max(attempt - 1, 0)),
        settings.yt_upload_chunk_backoff_max_seconds,
    )
    remaining = delay
    while remaining > 0:
        check_stop()
        step = min(_SLEEP_SLICE_SECONDS, remaining)
        _sleep(step)
        remaining -= step


class RangeReader:

    def __init__(self, url: str, size: int) -> None:
        self._url = url
        self._size = size
        self._http = requests.Session()

    def close(self) -> None:
        self._http.close()

    def read(self, begin: int, length: int) -> bytes:
        if begin >= self._size or length <= 0:
            return b""
        end = min(begin + length, self._size) - 1
        expected = end - begin + 1
        retries = settings.yt_upload_range_retries
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                response = self._http.get(
                    self._url,
                    headers={"Range": f"bytes={begin}-{end}"},
                    timeout=settings.yt_upload_range_timeout_seconds,
                )
            except requests.RequestException as exc:
                logger.warning("Reading the staged video failed: %s", type(exc).__name__)
                if last:
                    raise _source_error(
                        "source_unreadable",
                        "The video could not be read from storage.",
                        retryable=True,
                    ) from exc
                _sleep(min(2**attempt, 8))
                continue

            status = response.status_code
            if status == 206 or (status == 200 and begin == 0 and expected == self._size):
                data = response.content
                if len(data) == expected:
                    return data
                logger.warning("Staged video read returned %s of %s bytes", len(data), expected)
                if last:
                    raise _source_error(
                        "source_unreadable",
                        "The video could not be read from storage.",
                        retryable=True,
                    )
                _sleep(min(2**attempt, 8))
                continue
            if status in _TRANSIENT_STATUSES:
                if last:
                    raise _source_error(
                        "source_unreadable",
                        "The video could not be read from storage.",
                        retryable=True,
                    )
                _sleep(min(2**attempt, 8))
                continue
            if status == 404:
                raise _source_error(
                    "source_missing",
                    "The video file is no longer available. Start a new upload to publish it.",
                    retryable=False,
                )
            if status == 200:
                raise _source_error(
                    "source_unreadable",
                    "File storage does not support partial reads, so the video cannot be streamed.",
                    retryable=False,
                )
            match = _STORAGE_CODE.search(response.text[:2000])
            logger.error(
                "Staged video read was refused (status=%s, code=%s)",
                status,
                match.group(1) if match else "unknown",
            )
            raise _source_error(
                "source_unreadable",
                "The video could not be read from storage.",
                retryable=False,
            )
        raise _source_error(
            "source_unreadable", "The video could not be read from storage.", retryable=True
        )


def _refresh(credentials: Credentials) -> None:
    try:
        credentials.refresh(GoogleRequest())
    except RefreshError as exc:
        raise UploadFailureError(
            UploadFailure(
                code="reconnect_required",
                message=(
                    "YouTube no longer accepts the saved permission for this channel. "
                    "Reconnect the channel and try again."
                ),
                needs_reconnect=True,
            )
        ) from exc
    except TransportError as exc:
        raise _Transient("token refresh failed") from exc


def _token(credentials: Credentials) -> str:
    if not credentials.token or credentials.expired:
        _refresh(credentials)
    return str(credentials.token)


def _send(
    http: requests.Session,
    credentials: Credentials,
    method: str,
    url: str,
    *,
    headers: dict,
    data=None,
    params: Optional[dict] = None,
) -> requests.Response:
    refreshed = False
    while True:
        merged = {**headers, "Authorization": f"Bearer {_token(credentials)}"}
        try:
            response = http.request(
                method,
                url,
                headers=merged,
                data=data,
                params=params,
                timeout=settings.yt_upload_http_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise _Transient(
                type(exc).__name__, reached_server=not _never_connected(exc)
            ) from exc
        if response.status_code == 401 and not refreshed:
            refreshed = True
            _refresh(credentials)
            continue
        return response


def _parse_range(value: Optional[str]) -> int:
    if not value:
        return 0
    match = re.search(r"bytes=\d+-(\d+)", value)
    return int(match.group(1)) + 1 if match else 0


def _interpret(response: requests.Response) -> tuple[str, object]:
    status = response.status_code
    if status in (200, 201):
        try:
            video_id = response.json().get("id")
        except ValueError:
            video_id = None
        if not video_id:
            raise UploadFailureError(
                UploadFailure(
                    code="no_video_id",
                    message="YouTube finished the upload but did not return a video ID.",
                )
            )
        return "done", str(video_id)
    if status == 308:
        return "incomplete", _parse_range(response.headers.get("Range"))
    if status in (404, 410):
        raise SessionExpiredError()
    if status in _TRANSIENT_STATUSES:
        raise _Transient(f"status {status}", youtube=True)
    raise YouTubeHttpError(status, response.content)


def initiate_session(
    credentials: Credentials,
    *,
    body: dict,
    size_bytes: int,
    content_type: str,
    notify_subscribers: bool,
) -> str:
    http = requests.Session()
    try:
        response = _send(
            http,
            credentials,
            "POST",
            settings.yt_upload_api_url,
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": "true" if notify_subscribers else "false",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size_bytes),
                "X-Upload-Content-Type": content_type,
            },
            data=json.dumps(body).encode("utf-8"),
        )
    except _Transient as exc:
        raise UploadFailureError(
            UploadFailure(
                code="network_error",
                message="The connection was interrupted while uploading.",
                retryable=True,
                reached_server=exc.reached_server,
            )
        ) from exc
    finally:
        http.close()

    if response.status_code == 200:
        location = response.headers.get("Location")
        if location:
            return location
        raise UploadFailureError(
            UploadFailure(
                code="youtube_unavailable",
                message="YouTube had a temporary problem while uploading.",
                retryable=True,
            )
        )
    if response.status_code in _TRANSIENT_STATUSES:
        raise UploadFailureError(
            UploadFailure(
                code="youtube_unavailable",
                message="YouTube had a temporary problem while uploading.",
                retryable=True,
            )
        )
    raise YouTubeHttpError(response.status_code, response.content)


def _query_status(
    http: requests.Session, credentials: Credentials, session_uri: str, size_bytes: int
) -> tuple[str, object]:
    response = _send(
        http,
        credentials,
        "PUT",
        session_uri,
        headers={"Content-Length": "0", "Content-Range": f"bytes */{size_bytes}"},
    )
    return _interpret(response)


def _put_chunk(
    http: requests.Session,
    credentials: Credentials,
    session_uri: str,
    data: bytes,
    start: int,
    size_bytes: int,
) -> tuple[str, object]:
    response = _send(
        http,
        credentials,
        "PUT",
        session_uri,
        headers={
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {start}-{start + len(data) - 1}/{size_bytes}",
        },
        data=data,
    )
    return _interpret(response)


def upload_resumable(
    credentials: Credentials,
    *,
    session_uri: str,
    reader: RangeReader,
    size_bytes: int,
    on_progress: Callable[[int], None],
    check_stop: Callable[[], None],
) -> str:
    http = requests.Session()
    chunk = settings.yt_upload_chunk_bytes
    confirmed: Optional[int] = None
    failures = 0
    try:
        while True:
            check_stop()
            try:
                if confirmed is None:
                    kind, value = _query_status(http, credentials, session_uri, size_bytes)
                    if kind == "done":
                        on_progress(size_bytes)
                        return str(value)
                    confirmed = int(value)
                    on_progress(confirmed)
                if confirmed >= size_bytes:
                    raise _Transient("all bytes sent but the upload is not finalized")
                data = reader.read(confirmed, min(chunk, size_bytes - confirmed))
                kind, value = _put_chunk(
                    http, credentials, session_uri, data, confirmed, size_bytes
                )
                if kind == "done":
                    on_progress(size_bytes)
                    return str(value)
                moved = int(value) > confirmed
                confirmed = int(value)
                on_progress(confirmed)
                if moved:
                    failures = 0
                else:
                    raise _Transient("the server did not accept any bytes")
            except _Transient as exc:
                failures += 1
                logger.warning(
                    "Upload chunk failed (%s), attempt %s of %s",
                    exc,
                    failures,
                    settings.yt_upload_chunk_retries,
                )
                if failures > settings.yt_upload_chunk_retries:
                    if exc.youtube:
                        raise UploadFailureError(
                            UploadFailure(
                                code="youtube_unavailable",
                                message="YouTube had a temporary problem while uploading.",
                                retryable=True,
                            )
                        ) from exc
                    raise UploadFailureError(
                        UploadFailure(
                            code="network_error",
                            message="The connection was interrupted while uploading.",
                            retryable=True,
                        )
                    ) from exc
                _backoff(failures, check_stop)
                confirmed = None
    finally:
        http.close()


def _build_youtube(credentials: Credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def fetch_bytes_sync(url: str, *, max_bytes: int, timeout: int) -> bytes:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    data = response.content
    if len(data) > max_bytes:
        raise ValueError("The file is larger than allowed.")
    return data


def set_thumbnail_sync(
    credentials: Credentials, *, video_id: str, data: bytes, content_type: str
) -> None:
    youtube = _build_youtube(credentials)
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type, resumable=False)
    youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
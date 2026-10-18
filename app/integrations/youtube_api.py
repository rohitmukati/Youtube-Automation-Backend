# app/integrations/youtube_api.py
import io
import logging
from typing import Callable

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaUpload

from app.config import settings
from app.utils.yt_channel_management import UploadFailure, UploadFailureError

logger = logging.getLogger(__name__)

_SOURCE_UNREADABLE = "The video could not be read from storage."


def _source_error(message: str, *, retryable: bool) -> UploadFailureError:
    return UploadFailureError(
        UploadFailure(code="source_unreadable", message=message, retryable=retryable)
    )


class RangeMediaUpload(MediaUpload):
    def __init__(self, url: str, size: int, mimetype: str, chunksize: int, timeout: int) -> None:
        self._url = url
        self._size = size
        self._mimetype = mimetype
        self._chunksize = chunksize
        self._timeout = timeout

    def chunksize(self) -> int:
        return self._chunksize

    def mimetype(self) -> str:
        return self._mimetype

    def size(self) -> int:
        return self._size

    def resumable(self) -> bool:
        return True

    def has_stream(self) -> bool:
        return False

    def getbytes(self, begin: int, length: int) -> bytes:
        if begin >= self._size:
            return b""
        end = min(begin + length, self._size) - 1
        expected = end - begin + 1
        try:
            response = requests.get(
                self._url,
                headers={"Range": f"bytes={begin}-{end}"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Reading the source video failed: %s", exc)
            raise _source_error(_SOURCE_UNREADABLE, retryable=True) from exc

        if response.status_code == 206 or (
            response.status_code == 200 and begin == 0 and expected == self._size
        ):
            data = response.content
            if len(data) != expected:
                raise _source_error(_SOURCE_UNREADABLE, retryable=True)
            return data
        if response.status_code in (429, 500, 502, 503, 504):
            raise _source_error(_SOURCE_UNREADABLE, retryable=True)
        if response.status_code == 200:
            raise _source_error(
                "File storage does not support partial reads, so the video cannot be streamed.",
                retryable=False,
            )
        logger.error("Source video read failed with status %s", response.status_code)
        raise _source_error(_SOURCE_UNREADABLE, retryable=False)


def _build_youtube(credentials: Credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def upload_video_sync(
    credentials: Credentials,
    *,
    source_url: str,
    size_bytes: int,
    content_type: str,
    body: dict,
    notify_subscribers: bool,
    on_progress: Callable[[int], None],
) -> str:
    youtube = _build_youtube(credentials)
    media = RangeMediaUpload(
        url=source_url,
        size=size_bytes,
        mimetype=content_type,
        chunksize=settings.yt_upload_chunk_bytes,
        timeout=settings.yt_upload_range_timeout_seconds,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=notify_subscribers,
    )
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=settings.yt_upload_chunk_retries)
        if status is not None:
            on_progress(int(status.progress() * 100))

    video_id = response.get("id") if isinstance(response, dict) else None
    if not video_id:
        raise UploadFailureError(
            UploadFailure(
                code="no_video_id",
                message="YouTube finished the upload but did not return a video ID.",
            )
        )
    return video_id


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
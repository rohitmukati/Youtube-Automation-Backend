# app/integrations/ytdlp_extractor.py
"""Reads video details and the direct stream addresses with yt-dlp. Nothing is downloaded here."""
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.core.exceptions import (
    DownloaderBlockedError,
    DownloaderFailedError,
    DownloaderUnavailableError,
    InvalidVideoUrlError,
    VideoNotAvailableError,
)

logger = logging.getLogger(__name__)

_FORMAT_KEYS = (
    "format_id",
    "url",
    "ext",
    "protocol",
    "vcodec",
    "acodec",
    "width",
    "height",
    "fps",
    "tbr",
    "abr",
    "vbr",
    "filesize",
    "filesize_approx",
    "format_note",
)


@dataclass
class ExtractedVideo:
    video_id: str
    title: str
    uploader: Optional[str]
    duration: Optional[int]
    thumbnail: Optional[str]
    platform: str
    source_url: str
    is_live: bool
    formats: list[dict] = field(default_factory=list)


def _slim_format(fmt: dict[str, Any], default_headers: dict[str, str]) -> dict[str, Any]:
    slim = {key: fmt.get(key) for key in _FORMAT_KEYS}
    slim["http_headers"] = dict(fmt.get("http_headers") or default_headers)
    options = fmt.get("downloader_options") or {}
    slim["http_chunk_size"] = options.get("http_chunk_size")
    return slim


def _translate_error(message: str) -> Exception:
    text = message.lower()
    if ("confirm you" in text and "bot" in text) or "http error 429" in text or "too many requests" in text:
        return DownloaderBlockedError()
    if (
        "private" in text
        or "login required" in text
        or "rate-limit reached" in text
        or "sign in" in text
        or "log in" in text
        or "cookies" in text
    ):
        return VideoNotAvailableError(
            "This video is private, age-restricted or needs a login, so it cannot be downloaded."
        )
    if (
        "unavailable" in text
        or "not available" in text
        or "removed" in text
        or "does not exist" in text
        or "no video" in text
        or "terminated" in text
        or "copyright" in text
    ):
        return VideoNotAvailableError()
    if "unsupported url" in text:
        return InvalidVideoUrlError()
    return DownloaderFailedError()


def _build_options(cookie_copy: Optional[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "js_runtimes": {name: {} for name in settings.downloader_js_runtimes},
    }
    if settings.ytdlp_proxy:
        options["proxy"] = settings.ytdlp_proxy
    if cookie_copy:
        options["cookiefile"] = cookie_copy
    return options


def extract_sync(url: str, platform: str) -> ExtractedVideo:
    """Blocking call: run it in a worker thread."""
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError, ExtractorError
    except ImportError as exc:
        logger.error("yt-dlp is not installed")
        raise DownloaderUnavailableError() from exc

    # yt-dlp writes the cookie file back when it finishes, and secret files on some hosts
    # are read-only, so it always works on a throw-away copy.
    work_dir: Optional[str] = None
    cookie_copy: Optional[str] = None
    source = (settings.ytdlp_cookies_file or "").strip()
    if source and os.path.isfile(source):
        work_dir = tempfile.mkdtemp(prefix="ytdlp-")
        cookie_copy = os.path.join(work_dir, "cookies.txt")
        shutil.copyfile(source, cookie_copy)

    try:
        with yt_dlp.YoutubeDL(_build_options(cookie_copy)) as ydl:
            info = ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as exc:
        message = str(exc)
        logger.warning("Extraction failed for %s: %s", url, message[:300])
        raise _translate_error(message) from exc
    except Exception as exc:
        logger.exception("Unexpected extractor error for %s", url)
        raise DownloaderFailedError() from exc
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)

    if not info:
        raise VideoNotAvailableError()

    # A post can hold several videos (Instagram carousel): the first one is used.
    if info.get("_type") == "playlist" or info.get("entries"):
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise VideoNotAvailableError()
        info = entries[0]

    default_headers = dict(info.get("http_headers") or {})
    formats = [_slim_format(fmt, default_headers) for fmt in (info.get("formats") or [])]
    if not formats and info.get("url"):
        formats = [_slim_format(info, default_headers)]

    duration = info.get("duration")
    return ExtractedVideo(
        video_id=str(info.get("id") or ""),
        title=str(info.get("title") or info.get("fulltitle") or "video")[:300],
        uploader=info.get("uploader") or info.get("channel"),
        duration=int(duration) if duration else None,
        thumbnail=info.get("thumbnail"),
        platform=platform,
        source_url=url,
        is_live=bool(info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")),
        formats=formats,
    )
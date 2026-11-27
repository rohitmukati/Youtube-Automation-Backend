# app/utils/video_downloader.py
"""Pure helpers for the video downloader: URL checks, quality options, tokens and limits.

Nothing in this module touches the network, the database or the disk.
"""
import os
import re
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

import jwt

from app.config import settings
from app.core.exceptions import DownloadLinkExpiredError, InvalidVideoUrlError
from app.utils.datetime_utils import utc_now

DOWNLOAD_TOKEN_TYPE = "download"

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_PATH_ID = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})(?:[/?#]|$)")
_INSTAGRAM_PATH = re.compile(r"/(reel|reels|p|tv)/([A-Za-z0-9_-]{5,40})(?:/|$)")

_VIDEO_MEDIA_TYPES = {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime"}
_AUDIO_MEDIA_TYPES = {"m4a": "audio/mp4", "webm": "audio/webm"}


# ── URL validation ────────────────────────────────────────────────────────────


def _host_matches(host: str, allowed: list[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def validate_video_url(raw: str) -> tuple[str, str]:
    """Return (canonical_url, platform) or raise InvalidVideoUrlError.

    Only YouTube and Instagram links are accepted, so the server can never be used to
    fetch an arbitrary address. The canonical URL drops playlists and tracking parameters.
    """
    value = (raw or "").strip()
    if not value or len(value) > settings.downloader_url_max_length:
        raise InvalidVideoUrlError()
    if any(ord(char) < 32 for char in value):
        raise InvalidVideoUrlError()
    if "://" not in value:
        value = "https://" + value

    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise InvalidVideoUrlError() from exc

    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise InvalidVideoUrlError()
    if port not in (None, 80, 443):
        raise InvalidVideoUrlError()
    if not host or not _host_matches(host, settings.downloader_allowed_hosts):
        raise InvalidVideoUrlError(
            "Only YouTube and Instagram links are supported."
        )

    if _host_matches(host, ["youtu.be"]):
        video_id = parts.path.strip("/").split("/")[0]
        if _YOUTUBE_ID.match(video_id):
            return f"https://www.youtube.com/watch?v={video_id}", PLATFORM_YOUTUBE
        raise InvalidVideoUrlError()

    if _host_matches(host, ["youtube.com"]):
        if parts.path.rstrip("/") == "/watch":
            video_id = (parse_qs(parts.query).get("v") or [""])[0]
            if _YOUTUBE_ID.match(video_id):
                return f"https://www.youtube.com/watch?v={video_id}", PLATFORM_YOUTUBE
        match = _YOUTUBE_PATH_ID.match(parts.path)
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}", PLATFORM_YOUTUBE
        raise InvalidVideoUrlError(
            "Please paste the link of a single YouTube video, Short or live recording."
        )

    if _host_matches(host, ["instagram.com"]):
        match = _INSTAGRAM_PATH.search(parts.path)
        if match:
            kind = "reel" if match.group(1) in ("reel", "reels") else match.group(1)
            return f"https://www.instagram.com/{kind}/{match.group(2)}/", PLATFORM_INSTAGRAM
        raise InvalidVideoUrlError("Please paste the link of an Instagram reel or video post.")

    raise InvalidVideoUrlError()


def is_allowed_media_url(url: str) -> bool:
    """The stream URL given by the extractor must point at a known video CDN over HTTPS."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return parts.scheme == "https" and bool(host) and _host_matches(
        host, settings.downloader_cdn_hosts
    )


# ── File names ────────────────────────────────────────────────────────────────

_UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def build_filename(title: str, quality_label: str, ext: str) -> str:
    base = _UNSAFE_NAME_CHARS.sub(" ", title or "video")
    base = re.sub(r"\s+", " ", base).strip(" .") or "video"
    suffix = f" [{quality_label}]" if quality_label else ""
    limit = max(settings.downloader_filename_max_length - len(suffix), 10)
    return f"{base[:limit].rstrip(' .')}{suffix}.{ext}"


def content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace('"', "").strip() or "video"
    if ascii_name.startswith("["):
        ascii_name = "video " + ascii_name
    if not os.path.splitext(ascii_name)[1]:
        ascii_name += os.path.splitext(filename)[1] or ".mp4"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


# ── Quality options ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DownloadOptionSpec:
    id: str
    kind: str  # "video" | "audio"
    quality: int  # 0 for audio
    label: str
    ext: str
    media_type: str
    has_audio: bool
    needs_merge: bool
    size_bytes: Optional[int]
    video_format: dict = field(compare=False, default_factory=dict)
    audio_format: Optional[dict] = field(compare=False, default=None)


def _quality_of(fmt: dict) -> int:
    width, height = fmt.get("width"), fmt.get("height")
    if width and height:
        return int(min(width, height))
    return int(height or 0)


def _size_of(fmt: Optional[dict]) -> Optional[int]:
    if not fmt:
        return None
    value = fmt.get("filesize") or fmt.get("filesize_approx")
    return int(value) if value else None


def _is_http_format(fmt: dict) -> bool:
    return fmt.get("protocol") in ("https", "http") and bool(fmt.get("url"))


def _is_h264(fmt: dict) -> bool:
    codec = (fmt.get("vcodec") or "").lower()
    return codec.startswith(("avc1", "h264")) and fmt.get("ext") == "mp4"


def _is_m4a(fmt: dict) -> bool:
    return fmt.get("ext") == "m4a"


def _is_webm_audio(fmt: dict) -> bool:
    return fmt.get("ext") == "webm"


def _bitrate(fmt: dict) -> float:
    return float(fmt.get("tbr") or fmt.get("abr") or fmt.get("vbr") or 0)


def _best(candidates: list[dict]) -> Optional[dict]:
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda f: (f.get("ext") == "mp4", _bitrate(f), float(f.get("fps") or 0)),
    )


def _quality_label(quality: int, fmt: dict) -> str:
    fps = fmt.get("fps")
    suffix = str(int(fps)) if fps and fps > 30 else ""
    return f"{quality}p{suffix}"


def _too_big(size: Optional[int]) -> bool:
    return size is not None and size > settings.downloader_max_filesize_bytes


def build_options(formats: list[dict], *, ffmpeg_available: bool) -> list[DownloadOptionSpec]:
    """Turn the raw formats of a video into a short list of choices for the user.

    - A format that already has video and sound is offered as a direct download.
    - Higher qualities (video and sound are separate on YouTube) are offered only when
      ffmpeg is available, because the two streams are joined on the fly while streaming.
    - One audio-only choice is added when a pure audio stream exists.
    """
    usable = [f for f in formats if _is_http_format(f) and is_allowed_media_url(f["url"])]

    progressive: dict[int, list[dict]] = {}
    video_only: dict[int, list[dict]] = {}
    audio_only: list[dict] = []
    for fmt in usable:
        vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
        quality = _quality_of(fmt)
        if vcodec == "none":
            if acodec not in ("none", None):
                audio_only.append(fmt)
            continue
        if quality <= 0 or quality > settings.downloader_max_height:
            continue
        if fmt.get("ext") not in _VIDEO_MEDIA_TYPES:
            continue
        if acodec == "none":
            video_only.setdefault(quality, []).append(fmt)
        else:
            progressive.setdefault(quality, []).append(fmt)

    options: dict[int, DownloadOptionSpec] = {}

    for quality, group in progressive.items():
        best = _best(group)
        if best is None or _too_big(_size_of(best)):
            continue
        ext = best["ext"]
        options[quality] = DownloadOptionSpec(
            id=f"v{quality}",
            kind="video",
            quality=quality,
            label=_quality_label(quality, best),
            ext=ext,
            media_type=_VIDEO_MEDIA_TYPES[ext],
            has_audio=True,
            needs_merge=False,
            size_bytes=_size_of(best),
            video_format=best,
        )

    if ffmpeg_available:
        m4a = _best([f for f in audio_only if _is_m4a(f)])
        webm_audio = _best([f for f in audio_only if _is_webm_audio(f)])
        for quality, group in video_only.items():
            if quality in options:
                continue
            h264 = _best([f for f in group if _is_h264(f)])
            other = _best([f for f in group if f.get("ext") == "webm"])
            if h264 is not None and m4a is not None:
                video, audio, ext = h264, m4a, "mp4"
            elif other is not None and webm_audio is not None:
                video, audio, ext = other, webm_audio, "webm"
            else:
                continue
            total = (_size_of(video) or 0) + (_size_of(audio) or 0) or None
            if _too_big(total):
                continue
            options[quality] = DownloadOptionSpec(
                id=f"v{quality}",
                kind="video",
                quality=quality,
                label=_quality_label(quality, video),
                ext=ext,
                media_type=_VIDEO_MEDIA_TYPES[ext],
                has_audio=True,
                needs_merge=True,
                size_bytes=total,
                video_format=video,
                audio_format=audio,
            )

    result = [options[quality] for quality in sorted(options, reverse=True)]

    audio = _best([f for f in audio_only if _is_m4a(f)]) or _best(
        [f for f in audio_only if _is_webm_audio(f)]
    )
    if audio is not None and not _too_big(_size_of(audio)):
        ext = audio["ext"]
        result.append(
            DownloadOptionSpec(
                id="a",
                kind="audio",
                quality=0,
                label=f"Audio only ({ext.upper()})",
                ext=ext,
                media_type=_AUDIO_MEDIA_TYPES[ext],
                has_audio=True,
                needs_merge=False,
                size_bytes=_size_of(audio),
                video_format=audio,
            )
        )
    return result


def find_option(options: list[DownloadOptionSpec], option_id: str) -> Optional[DownloadOptionSpec]:
    return next((option for option in options if option.id == option_id), None)


# ── ffmpeg ────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def find_ffmpeg() -> Optional[str]:
    configured = (settings.ffmpeg_path or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # a static ffmpeg binary shipped as a pip package (works on hosts without apt)
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
        return path if path and os.path.isfile(path) else None
    except Exception:
        return None


# ── Download token ────────────────────────────────────────────────────────────


def create_download_token(user_id: uuid.UUID, url: str, option_id: str) -> tuple[str, int]:
    issued_at = utc_now()
    lifetime = timedelta(seconds=settings.downloader_token_ttl_seconds)
    payload = {
        "sub": str(user_id),
        "type": DOWNLOAD_TOKEN_TYPE,
        "jti": uuid.uuid4().hex,
        "url": url,
        "opt": option_id,
        "iat": issued_at,
        "exp": issued_at + lifetime,
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, int(lifetime.total_seconds())


def decode_download_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "type", "url", "opt"]},
        )
    except jwt.PyJWTError as exc:
        raise DownloadLinkExpiredError() from exc
    if payload.get("type") != DOWNLOAD_TOKEN_TYPE:
        raise DownloadLinkExpiredError()
    return payload


# ── Limits (in memory: the app runs as one process) ───────────────────────────


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        if len(self._hits) > 5000:
            self._hits = {k: v for k, v in self._hits.items() if v}
        return True


class StreamGate:
    """Caps how many downloads stream at the same time on this server.

    A running download keeps its slot alive by calling `touch`. A slot that shows no
    activity for `idle_seconds` is dropped, so a download that ended in an unusual way
    can never block the server.
    """

    def __init__(self, max_total: int, idle_seconds: float = 180) -> None:
        self._max_total = max_total
        self._idle_seconds = idle_seconds
        self._active: dict[str, float] = {}

    def _purge(self) -> None:
        limit = time.monotonic() - self._idle_seconds
        for ticket in [t for t, last in self._active.items() if last < limit]:
            self._active.pop(ticket, None)

    def acquire(self) -> Optional[str]:
        self._purge()
        if len(self._active) >= self._max_total:
            return None
        ticket = uuid.uuid4().hex
        self._active[ticket] = time.monotonic()
        return ticket

    def touch(self, ticket: Optional[str]) -> None:
        if ticket in self._active:
            self._active[ticket] = time.monotonic()

    def release(self, ticket: Optional[str]) -> None:
        if ticket:
            self._active.pop(ticket, None)
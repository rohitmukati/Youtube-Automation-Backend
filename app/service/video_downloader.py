# app/service/video_downloader.py
"""Stream-through video downloader.

The video is never written to disk or to the bucket: the bytes travel from YouTube or
Instagram through this server straight to the user's browser.
"""
import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.core.exceptions import (
    DownloaderBlockedError,
    DownloaderBusyError,
    DownloaderFailedError,
    DownloaderUnavailableError,
    VideoNotAllowedError,
    VideoNotAvailableError,
)
from app.integrations.ytdlp_extractor import ExtractedVideo, extract_sync
from app.schemas.video_downloader import (
    DownloadOption,
    PrepareDownloadRequest,
    PrepareDownloadResponse,
    VideoInfoRequest,
    VideoInfoResponse,
)
from app.utils.video_downloader import (
    DownloadOptionSpec,
    SlidingWindowLimiter,
    StreamGate,
    build_filename,
    build_options,
    content_disposition,
    create_download_token,
    decode_download_token,
    find_ffmpeg,
    find_option,
    is_allowed_media_url,
    validate_video_url,
)

logger = logging.getLogger(__name__)

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
_DROP_HEADERS = {"range", "host", "accept-encoding", "content-length", "connection"}
_READ_BLOCK = 64 * 1024
_MAX_REDIRECTS = 4

# One process serves the app, so plain module level state is enough.
_cache: dict[str, tuple[float, ExtractedVideo]] = {}
_extract_slots = asyncio.Semaphore(settings.downloader_extract_concurrency)
_info_limiter = SlidingWindowLimiter(settings.downloader_info_per_minute, 60)
_prepare_limiter = SlidingWindowLimiter(settings.downloader_prepare_per_minute, 60)
_gate = StreamGate(
    settings.downloader_max_concurrent_streams, settings.downloader_stream_idle_seconds
)
_background: set[asyncio.Task] = set()


def _run_in_background(coroutine) -> None:
    """Run cleanup that must finish even when the download task itself is being cancelled."""
    task = asyncio.ensure_future(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _close_quietly(*closers) -> None:
    for closer in closers:
        try:
            await closer()
        except Exception:
            pass


@dataclass
class StreamPlan:
    iterator: AsyncIterator[bytes]
    media_type: str
    disposition: str
    content_length: Optional[int]


def _to_option(spec: DownloadOptionSpec) -> DownloadOption:
    return DownloadOption(
        id=spec.id,
        kind=spec.kind,  # type: ignore[arg-type]
        label=spec.label,
        ext=spec.ext,
        quality=spec.quality,
        has_audio=spec.has_audio,
        size_bytes=spec.size_bytes,
        needs_processing=spec.needs_merge,
    )


def _file_label(spec: DownloadOptionSpec) -> str:
    return spec.label if spec.kind == "video" else "audio"


def _clean_headers(headers: Optional[dict]) -> dict[str, str]:
    cleaned = {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() not in _DROP_HEADERS
    }
    cleaned["Accept-Encoding"] = "identity"
    return cleaned


class VideoDownloaderService:
    # ── Public API ────────────────────────────────────────────────────────────

    async def get_info(self, user_id: uuid.UUID, payload: VideoInfoRequest) -> VideoInfoResponse:
        self._ensure_enabled()
        if not _info_limiter.allow(str(user_id)):
            raise DownloaderBusyError("Too many links checked in a short time. Please wait a minute.")
        url, platform = validate_video_url(payload.url)
        video, options = await self._load(url, platform)
        return VideoInfoResponse(
            platform=platform,  # type: ignore[arg-type]
            source_url=url,
            title=video.title,
            uploader=video.uploader,
            duration_seconds=video.duration,
            thumbnail_url=video.thumbnail if (video.thumbnail or "").startswith("https://") else None,
            options=[_to_option(option) for option in options],
        )

    async def prepare(
        self, user_id: uuid.UUID, payload: PrepareDownloadRequest
    ) -> PrepareDownloadResponse:
        self._ensure_enabled()
        if not _prepare_limiter.allow(str(user_id)):
            raise DownloaderBusyError("Too many downloads started in a short time. Please wait a minute.")
        url, platform = validate_video_url(payload.url)
        video, options = await self._load(url, platform)
        option = find_option(options, payload.option_id)
        if option is None:
            raise VideoNotAvailableError("This quality is not available. Please choose another one.")
        token, expires_in = create_download_token(user_id, url, option.id)
        filename = build_filename(video.title, _file_label(option), option.ext)
        return PrepareDownloadResponse(token=token, filename=filename, expires_in=expires_in)

    async def open_stream(self, token: str) -> StreamPlan:
        """Validate the link, open the source and return an iterator of bytes.

        The source is opened here, before the response starts, so that a failure is still
        reported as a normal error and not as a broken download.
        """
        self._ensure_enabled()
        claims = decode_download_token(token)
        url, platform = validate_video_url(claims["url"])
        video, options = await self._load(url, platform)
        option = find_option(options, claims["opt"])
        if option is None:
            raise VideoNotAvailableError("This quality is not available any more.")

        ticket = _gate.acquire()
        if ticket is None:
            raise DownloaderBusyError(
                "The server is busy with other downloads right now. Please try again in a minute."
            )

        try:
            if option.needs_merge:
                iterator, length = await self._open_merge(option, ticket)
            else:
                iterator, length = await self._open_proxy(option.video_format, ticket)
        except BaseException:
            _gate.release(ticket)
            raise

        filename = build_filename(video.title, _file_label(option), option.ext)
        logger.info(
            "DOWNLOAD_START user=%s platform=%s option=%s merge=%s",
            claims["sub"],
            platform,
            option.id,
            option.needs_merge,
        )
        return StreamPlan(
            iterator=iterator,
            media_type=option.media_type,
            disposition=content_disposition(filename),
            content_length=length,
        )

    # ── Loading and caching ───────────────────────────────────────────────────

    @staticmethod
    def _ensure_enabled() -> None:
        if not settings.downloader_enabled:
            raise DownloaderUnavailableError()

    async def _load(
        self, url: str, platform: str
    ) -> tuple[ExtractedVideo, list[DownloadOptionSpec]]:
        video = await self._extract(url, platform)
        if video.is_live:
            raise VideoNotAllowedError("Live streams cannot be downloaded.")
        if video.duration and video.duration > settings.downloader_max_duration_seconds:
            hours = settings.downloader_max_duration_seconds // 3600
            raise VideoNotAllowedError(f"Videos longer than {hours} hours cannot be downloaded.")
        options = build_options(video.formats, ffmpeg_available=find_ffmpeg() is not None)
        if not options:
            raise VideoNotAvailableError(
                "No downloadable quality was found for this video."
            )
        return video, options

    async def _extract(self, url: str, platform: str) -> ExtractedVideo:
        now = time.monotonic()
        cached = _cache.get(url)
        if cached and cached[0] > now:
            return cached[1]

        try:
            await asyncio.wait_for(_extract_slots.acquire(), timeout=20)
        except asyncio.TimeoutError as exc:
            raise DownloaderBusyError() from exc
        try:
            video = await asyncio.wait_for(
                asyncio.to_thread(extract_sync, url, platform),
                timeout=settings.downloader_extract_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            logger.warning("Extraction timed out for %s", url)
            raise DownloaderFailedError() from exc
        finally:
            _extract_slots.release()

        if len(_cache) >= settings.downloader_info_cache_max_items:
            for key in [k for k, (expires, _) in _cache.items() if expires <= now]:
                _cache.pop(key, None)
            while len(_cache) >= settings.downloader_info_cache_max_items:
                _cache.pop(next(iter(_cache)), None)
        _cache[url] = (now + settings.downloader_info_cache_seconds, video)
        return video

    # ── Direct proxy (video that already has sound, or audio only) ────────────

    async def _open_proxy(
        self, fmt: dict, ticket: str
    ) -> tuple[AsyncIterator[bytes], Optional[int]]:
        url = fmt["url"]
        headers = _clean_headers(fmt.get("http_headers"))
        chunk = settings.downloader_chunk_bytes
        timeout = httpx.Timeout(
            connect=settings.downloader_connect_timeout_seconds,
            read=settings.downloader_read_timeout_seconds,
            write=15,
            pool=15,
        )
        client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, proxy=settings.ytdlp_proxy or None
        )

        async def fetch(source: str, start: int, end: Optional[int]) -> tuple[httpx.Response, str]:
            window = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
            current = source
            for _ in range(_MAX_REDIRECTS):
                request = client.build_request("GET", current, headers={**headers, "Range": window})
                response = await client.send(request, stream=True)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    await response.aclose()
                    current = urljoin(current, location or "")
                    if not location or not is_allowed_media_url(current):
                        raise DownloaderFailedError()
                    continue
                return response, current
            raise DownloaderFailedError()

        try:
            first, current_url = await fetch(url, 0, chunk - 1)
        except DownloaderFailedError:
            await client.aclose()
            raise
        except httpx.HTTPError as exc:
            await client.aclose()
            logger.warning("Could not open the video source: %s", exc.__class__.__name__)
            raise DownloaderFailedError() from exc

        if first.status_code not in (200, 206):
            status_code = first.status_code
            await first.aclose()
            await client.aclose()
            logger.warning("Video source answered with HTTP %s", status_code)
            if status_code in (403, 429):
                raise DownloaderBlockedError()
            raise DownloaderFailedError()

        total: Optional[int] = None
        whole_body = first.status_code == 200
        if whole_body:
            length = first.headers.get("content-length")
            total = int(length) if length and length.isdigit() else None
        else:
            match = _CONTENT_RANGE.match(first.headers.get("content-range", ""))
            if match and match.group(3) != "*":
                total = int(match.group(3))

        if total is not None and total > settings.downloader_max_filesize_bytes:
            await first.aclose()
            await client.aclose()
            raise VideoNotAllowedError("This file is too large to download here.")

        async def body() -> AsyncIterator[bytes]:
            response: Optional[httpx.Response] = first
            sent = 0
            try:
                while response is not None:
                    async for block in response.aiter_bytes(_READ_BLOCK):
                        sent += len(block)
                        _gate.touch(ticket)
                        yield block
                    await response.aclose()
                    response = None
                    if whole_body or total is None or sent >= total:
                        break
                    end = min(sent + chunk, total) - 1
                    response, _ = await fetch(current_url, sent, end)
                    if response.status_code != 206:
                        logger.warning(
                            "Video source stopped at %s of %s bytes (HTTP %s)",
                            sent,
                            total,
                            response.status_code,
                        )
                        break
            except (httpx.HTTPError, DownloaderFailedError) as exc:
                logger.warning("Download stopped early: %s", exc.__class__.__name__)
            finally:
                # Free the slot first: when the browser cancels, the awaits below can be
                # cancelled too, so nothing that must happen may come after them.
                _gate.release(ticket)
                closers = [client.aclose]
                if response is not None:
                    closers.insert(0, response.aclose)
                _run_in_background(_close_quietly(*closers))

        return body(), total

    # ── Join separate video and audio streams with ffmpeg ─────────────────────

    @staticmethod
    def _ffmpeg_input(fmt: dict) -> list[str]:
        headers = fmt.get("http_headers") or {}
        args = ["-protocol_whitelist", "https,tls,tcp,crypto"]
        agent = headers.get("User-Agent")
        if agent:
            args += ["-user_agent", agent]
        extra = "".join(
            f"{key}: {value}\r\n"
            for key, value in headers.items()
            if key.lower() not in _DROP_HEADERS and key.lower() != "user-agent"
        )
        if extra:
            args += ["-headers", extra]
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", fmt["url"],
        ]
        return args

    async def _open_merge(
        self, spec: DownloadOptionSpec, ticket: str
    ) -> tuple[AsyncIterator[bytes], Optional[int]]:
        ffmpeg = find_ffmpeg()
        if ffmpeg is None or spec.audio_format is None:
            raise DownloaderUnavailableError()

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            *self._ffmpeg_input(spec.video_format),
            *self._ffmpeg_input(spec.audio_format),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c", "copy",
        ]
        if spec.ext == "mp4":
            command += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
        else:
            command += ["-f", "webm"]
        command.append("pipe:1")

        env = dict(os.environ)
        if settings.ytdlp_proxy:
            env["http_proxy"] = settings.ytdlp_proxy
            env["https_proxy"] = settings.ytdlp_proxy

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert process.stdout is not None and process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())

        def kill() -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        async def reap() -> None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass
            stderr_task.cancel()

        async def stop() -> None:
            kill()
            await reap()

        try:
            first = await asyncio.wait_for(
                process.stdout.read(_READ_BLOCK),
                timeout=settings.downloader_extract_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await stop()
            raise DownloaderFailedError() from exc
        if not first:
            await stop()
            logger.warning("ffmpeg produced no data")
            raise DownloaderFailedError()

        async def body() -> AsyncIterator[bytes]:
            sent = len(first)
            try:
                yield first
                while True:
                    block = await process.stdout.read(_READ_BLOCK)  # type: ignore[union-attr]
                    if not block:
                        break
                    sent += len(block)
                    _gate.touch(ticket)
                    if sent > settings.downloader_max_filesize_bytes:
                        logger.warning("Download stopped: size limit reached")
                        break
                    yield block
                code = await asyncio.wait_for(process.wait(), timeout=10)
                if code != 0:
                    detail = b""
                    if stderr_task.done() and not stderr_task.cancelled():
                        detail = stderr_task.result()
                    logger.warning(
                        "ffmpeg ended with code %s: %s",
                        code,
                        detail.decode("utf-8", "ignore")[:300],
                    )
            except asyncio.TimeoutError:
                logger.warning("ffmpeg did not exit in time")
            finally:
                _gate.release(ticket)
                kill()
                _run_in_background(reap())

        return body(), None
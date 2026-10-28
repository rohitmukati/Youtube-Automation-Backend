# app/utils/yt_media_upload.py
import enum
import math
import uuid
from datetime import datetime, timedelta
from pathlib import PurePath
from typing import Iterable, Optional

from app.config import settings

_BYTES_PER_GB = 1024**3
_BYTES_PER_MB = 1024**2

_EXTENSION_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}
_MP4_BRAND_ATOMS = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot")


class MediaAssetStatus(str, enum.Enum):
    UPLOADING = "uploading"
    READY = "ready"
    DELETED = "deleted"
    ABORTED = "aborted"


LIVE_MEDIA_STATUSES = (MediaAssetStatus.UPLOADING.value, MediaAssetStatus.READY.value)


def format_size(size_bytes: int) -> str:
    if size_bytes >= _BYTES_PER_GB:
        value = size_bytes / _BYTES_PER_GB
        unit = "GB"
    else:
        value = size_bytes / _BYTES_PER_MB
        unit = "MB"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def clean_file_name(raw: Optional[str]) -> str:
    name = PurePath((raw or "").replace("\\", "/")).name.strip()
    name = "".join(char for char in name if ord(char) >= 32)
    return name[: settings.media_file_name_max_length]


def file_extension(name: str) -> str:
    return PurePath(name).suffix.lower()


def resolve_video_content_type(name: str, declared: Optional[str]) -> str:
    extension = file_extension(name)
    if extension not in settings.media_video_extensions:
        raise ValueError(
            "Only video files (MP4, MOV, AVI, MKV, WEBM, MPEG) can be published."
        )
    content_type = (declared or "").split(";")[0].strip().lower()
    if content_type in settings.media_video_content_types:
        return content_type
    if content_type in ("", "application/octet-stream"):
        return _EXTENSION_CONTENT_TYPES[extension]
    raise ValueError(
        "Only video files (MP4, MOV, AVI, MKV, WEBM, MPEG) can be published."
    )


def check_video_size(size_bytes: int) -> None:
    if size_bytes <= 0:
        raise ValueError("The video file is empty.")
    if size_bytes > settings.media_video_max_bytes:
        raise ValueError(
            f"Videos can be at most {format_size(settings.media_video_max_bytes)}."
        )


def build_object_key(user_id: uuid.UUID, asset_id: uuid.UUID, extension: str) -> str:
    return f"{settings.media_staging_prefix}/{user_id}/{asset_id}{extension}"


def total_parts(size_bytes: int, part_size: Optional[int] = None) -> int:
    size = part_size or settings.media_part_size_bytes
    return max(1, math.ceil(size_bytes / size))


def expected_part_size(
    part_number: int, size_bytes: int, part_size: Optional[int] = None
) -> int:
    size = part_size or settings.media_part_size_bytes
    count = total_parts(size_bytes, size)
    if part_number < 1 or part_number > count:
        return 0
    if part_number < count:
        return size
    return size_bytes - size * (count - 1)


def upload_deadline(now: datetime) -> datetime:
    return now + timedelta(hours=settings.media_upload_window_hours)


def retention_deadline(now: datetime) -> datetime:
    return now + timedelta(hours=settings.media_retention_hours)


def find_part_problems(
    parts: Iterable, size_bytes: int, part_size: Optional[int] = None
) -> tuple[list[int], list[int]]:
    count = total_parts(size_bytes, part_size)
    seen = {part.part_number: part for part in parts}
    missing = [number for number in range(1, count + 1) if number not in seen]
    wrong = [
        number
        for number in range(1, count + 1)
        if number in seen
        and seen[number].size != expected_part_size(number, size_bytes, part_size)
    ]
    return missing, wrong


def looks_like_video(header: bytes) -> bool:
    if len(header) < 12:
        return False
    if header[4:8] in _MP4_BRAND_ATOMS:
        return True
    if header[:4] == b"\x1a\x45\xdf\xa3":
        return True
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return True
    if header[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return True
    return False


def looks_like_image(header: bytes, content_type: str) -> bool:
    if content_type == "image/jpeg":
        return header[:3] == b"\xff\xd8\xff"
    if content_type == "image/png":
        return header[:8] == b"\x89PNG\r\n\x1a\n"
    if content_type == "image/gif":
        return header[:6] in (b"GIF87a", b"GIF89a")
    if content_type == "image/webp":
        return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False
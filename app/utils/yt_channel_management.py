# app/utils/yt_channel_management.py
import enum
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings
from app.utils.datetime_utils import ensure_utc


class PrivacyStatus(str, enum.Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"


class UploadJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UploadTargetStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ThumbnailStatus(str, enum.Enum):
    NONE = "none"
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


WAITING_TARGET_STATUSES = (
    UploadTargetStatus.PENDING.value,
    UploadTargetStatus.RETRYING.value,
)
ACTIVE_TARGET_STATUSES = (
    UploadTargetStatus.PENDING.value,
    UploadTargetStatus.UPLOADING.value,
    UploadTargetStatus.RETRYING.value,
)


@dataclass(frozen=True)
class UploadFailure:
    code: str
    message: str
    retryable: bool = False
    stop_all: bool = False
    needs_reconnect: bool = False


class UploadFailureError(Exception):
    def __init__(self, failure: UploadFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class UploadCancelled(Exception):
    pass


class UploadInterrupted(Exception):
    pass


QUOTA_EXCEEDED_FAILURE = UploadFailure(
    code="quota_exceeded",
    message=(
        "The daily YouTube upload limit for this app has been reached. "
        "Retry after the limit resets at midnight Pacific Time."
    ),
    stop_all=True,
)

_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_TRANSIENT_REASONS = {
    "backendError",
    "internalError",
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "serviceUnavailable",
}
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_AUTH_REASONS = {"authError", "insufficientPermissions", "forbiddenForUserAuth"}
_METADATA_REASONS = {
    "invalidTitle",
    "invalidDescription",
    "invalidTags",
    "invalidCategoryId",
    "invalidPublishAt",
    "invalidVideoMetadata",
    "defaultLanguageNotSupported",
}
_NETWORK_MODULES = ("httplib2", "urllib3", "requests", "ssl", "socket")


def clean_title(value: str) -> str:
    title = " ".join(value.split())
    if not title:
        raise ValueError("The title cannot be empty.")
    if len(title) > settings.yt_title_max_length:
        raise ValueError(
            f"The title can be at most {settings.yt_title_max_length} characters long."
        )
    if "<" in title or ">" in title:
        raise ValueError("The title cannot contain the characters < or >.")
    return title


def clean_description(value: str) -> str:
    text = value.replace("\r\n", "\n").strip()
    if "<" in text or ">" in text:
        raise ValueError("The description cannot contain the characters < or >.")
    if len(text.encode("utf-8")) > settings.yt_description_max_bytes:
        raise ValueError(
            f"The description can be at most {settings.yt_description_max_bytes} bytes long."
        )
    return text


def clean_tags(values: Iterable[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = " ".join(value.split())
        if not tag:
            continue
        if any(char in tag for char in ",<>") or any(ord(char) < 32 for char in tag):
            raise ValueError(
                "Tags cannot contain commas, angle brackets or control characters."
            )
        if len(tag) > settings.yt_tag_max_length:
            raise ValueError(
                f"Each tag can be at most {settings.yt_tag_max_length} characters long."
            )
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    if len(tags) > settings.yt_max_tags:
        raise ValueError(f"A video can have at most {settings.yt_max_tags} tags.")
    total = sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(len(tags) - 1, 0)
    if total > settings.yt_tags_max_total_chars:
        raise ValueError(
            f"All tags together can be at most {settings.yt_tags_max_total_chars} characters long."
        )
    return tags


def compute_job_status(statuses: Iterable[str]) -> UploadJobStatus:
    counts = Counter(statuses)
    total = sum(counts.values())
    if total == 0:
        return UploadJobStatus.QUEUED
    active = sum(counts[value] for value in ACTIVE_TARGET_STATUSES)
    if active:
        untouched = counts[UploadTargetStatus.PENDING.value]
        if untouched == total:
            return UploadJobStatus.QUEUED
        return UploadJobStatus.RUNNING
    done = counts[UploadTargetStatus.DONE.value]
    if done == total:
        return UploadJobStatus.COMPLETED
    if done > 0:
        return UploadJobStatus.PARTIALLY_COMPLETED
    if counts[UploadTargetStatus.FAILED.value] == 0:
        return UploadJobStatus.CANCELLED
    return UploadJobStatus.FAILED


def _quota_zone():
    try:
        return ZoneInfo(settings.yt_upload_quota_timezone)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-8))


def quota_day(moment: datetime) -> date:
    return ensure_utc(moment).astimezone(_quota_zone()).date()


def next_quota_reset(moment: datetime) -> datetime:
    zone = _quota_zone()
    local = ensure_utc(moment).astimezone(zone)
    midnight = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=zone)
    return midnight.astimezone(timezone.utc)


def retry_delay_seconds(failed_attempts: int) -> int:
    delays = settings.yt_upload_retry_delays_seconds
    if not delays:
        return 0
    index = min(max(failed_attempts - 1, 0), len(delays) - 1)
    return delays[index]


def watch_url(video_id: str) -> str:
    return settings.yt_watch_url_template.format(video_id=video_id)


def shorten(message: Optional[str]) -> Optional[str]:
    if message is None:
        return None
    limit = settings.yt_upload_error_max_length
    return message if len(message) <= limit else message[: limit - 3] + "..."


def build_video_body(
    *,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    privacy_status: str,
    publish_at: Optional[datetime],
    made_for_kids: bool,
    default_language: Optional[str],
) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": title,
        "description": description,
        "categoryId": category_id,
    }
    if tags:
        snippet["tags"] = tags
    if default_language:
        snippet["defaultLanguage"] = default_language
    status: dict[str, Any] = {
        "privacyStatus": privacy_status,
        "selfDeclaredMadeForKids": made_for_kids,
    }
    if publish_at is not None:
        status["publishAt"] = ensure_utc(publish_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"snippet": snippet, "status": status}


def http_status(exc: BaseException) -> Optional[int]:
    raw = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _http_reason(exc: BaseException) -> str:
    content = getattr(exc, "content", None)
    if not content:
        return ""
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason", ""))
    return str(error.get("status", ""))


def _is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    module = type(exc).__module__ or ""
    return module.startswith(_NETWORK_MODULES) or type(exc).__name__ == "TransportError"


def classify_error(exc: BaseException) -> UploadFailure:
    if isinstance(exc, UploadFailureError):
        return exc.failure

    status = http_status(exc)
    if status is not None and hasattr(exc, "content"):
        reason = _http_reason(exc)
        if reason in _QUOTA_REASONS:
            return QUOTA_EXCEEDED_FAILURE
        if reason == "uploadLimitExceeded":
            return UploadFailure(
                code="channel_upload_limit",
                message="This channel has reached its daily YouTube upload limit. Try again tomorrow.",
            )
        if reason == "youtubeSignupRequired":
            return UploadFailure(
                code="no_youtube_channel",
                message="This Google account does not have a YouTube channel to upload to.",
            )
        if status == 401 or reason in _AUTH_REASONS:
            return UploadFailure(
                code="reconnect_required",
                message=(
                    "YouTube no longer accepts the saved permission for this channel. "
                    "Reconnect the channel and try again."
                ),
                needs_reconnect=True,
            )
        if reason in _METADATA_REASONS:
            return UploadFailure(
                code="invalid_video_details",
                message=(
                    f"YouTube rejected the video details ({reason}). "
                    "Please review the title, description and tags."
                ),
            )
        if status in _TRANSIENT_STATUSES or reason in _TRANSIENT_REASONS:
            return UploadFailure(
                code="youtube_unavailable",
                message="YouTube had a temporary problem while uploading.",
                retryable=True,
            )
        if status == 403:
            return UploadFailure(
                code="forbidden",
                message="YouTube refused the upload for this channel. Check that the channel is allowed to upload videos.",
            )
        return UploadFailure(
            code="rejected_by_youtube",
            message=f"YouTube rejected the upload (status {status}). Please review the video details.",
        )

    if _is_network_error(exc):
        return UploadFailure(
            code="network_error",
            message="The connection was interrupted while uploading.",
            retryable=True,
        )

    return UploadFailure(
        code="unexpected_error",
        message="Something unexpected went wrong while uploading. Please try again.",
    )


def thumbnail_failure_message(exc: BaseException) -> str:
    if http_status(exc) == 403:
        return (
            "YouTube did not accept the custom thumbnail. "
            "The channel may need to be verified to use custom thumbnails."
        )
    return "The thumbnail could not be set. The video itself was uploaded."
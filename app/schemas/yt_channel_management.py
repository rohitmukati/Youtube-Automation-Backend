# app/schemas/yt_channel_management.py
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.schemas.yt_media_upload import DeclaredFile, MediaUploadInfo
from app.utils.datetime_utils import utc_now
from app.utils.yt_media_upload import MediaAssetStatus
from app.utils.yt_channel_management import (
    PrivacyStatus,
    ThumbnailStatus,
    UploadJobStatus,
    UploadTargetStatus,
    clean_description,
    clean_tags,
    clean_title,
)


class CreateUploadRequest(BaseModel):
    file: DeclaredFile
    client_request_id: Optional[str] = Field(
        default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    account_ids: list[uuid.UUID] = Field(
        min_length=1, max_length=settings.yt_upload_max_accounts
    )
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category_id: str = Field(default=settings.yt_default_category_id, pattern=r"^\d{1,3}$")
    privacy_status: PrivacyStatus = PrivacyStatus(settings.yt_default_privacy)
    publish_at: Optional[datetime] = None
    made_for_kids: bool = False
    default_language: Optional[str] = Field(
        default=None, max_length=20, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"
    )
    notify_subscribers: bool = True
    thumbnail_file_id: Optional[uuid.UUID] = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return clean_title(value)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return clean_description(value)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return clean_tags(value)

    @field_validator("account_ids")
    @classmethod
    def dedupe_accounts(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(value))

    @field_validator("publish_at")
    @classmethod
    def validate_publish_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return value
        if value.tzinfo is None:
            raise ValueError(
                "The publish time must include a timezone, for example 2026-12-01T10:00:00+05:30."
            )
        if value <= utc_now():
            raise ValueError("The publish time must be in the future.")
        return value

    @model_validator(mode="after")
    def scheduled_videos_stay_private(self) -> "CreateUploadRequest":
        if self.publish_at is not None:
            self.privacy_status = PrivacyStatus.PRIVATE
        return self


class UploadTargetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: Optional[uuid.UUID] = None
    account_nickname: str
    account_email: Optional[str] = None
    channel_id: str
    channel_title: str
    status: UploadTargetStatus
    progress_percent: int
    attempts: int
    bytes_confirmed: int = 0
    max_attempts: int = settings.yt_upload_max_attempts
    next_attempt_at: Optional[datetime] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    thumbnail_status: ThumbnailStatus
    thumbnail_error: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class UploadJobSummary(BaseModel):
    id: uuid.UUID
    status: UploadJobStatus
    title: str
    privacy_status: PrivacyStatus
    media_status: Optional[MediaAssetStatus] = None
    media_expires_at: Optional[datetime] = None
    source_file_name: str
    source_size_bytes: int
    total_channels: int
    done_count: int
    failed_count: int
    cancelled_count: int
    active_count: int
    created_at: datetime
    updated_at: datetime


class UploadJobDetail(UploadJobSummary):
    description: str
    tags: list[str]
    category_id: str
    publish_at: Optional[datetime] = None
    made_for_kids: bool
    default_language: Optional[str] = None
    notify_subscribers: bool
    source_content_type: str
    thumbnail_file_id: Optional[uuid.UUID] = None
    thumbnail_file_name: Optional[str] = None
    targets: list[UploadTargetResponse]


class StartUploadResponse(BaseModel):
    job: "UploadJobDetail"
    upload: Optional[MediaUploadInfo] = None
    reused: bool = False


class UploadChannelBrief(BaseModel):
    account_id: Optional[uuid.UUID] = None
    account_nickname: str
    account_email: Optional[str] = None
    channel_title: str
    status: UploadTargetStatus
    youtube_url: Optional[str] = None


class UploadJobListItem(UploadJobSummary):
    channels: list[UploadChannelBrief]


class UploadJobListResponse(BaseModel):
    jobs: list[UploadJobListItem]
    total: int
    limit: int
    offset: int


class UploadQuotaResponse(BaseModel):
    daily_limit: int
    used_today: int
    waiting: int
    remaining: int
    resets_at: datetime


class MessageResponse(BaseModel):
    message: str
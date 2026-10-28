# app/schemas/yt_media_upload.py
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.config import settings
from app.utils.yt_media_upload import (
    MediaAssetStatus,
    check_video_size,
    clean_file_name,
    resolve_video_content_type,
)


class DeclaredFile(BaseModel):
    name: str = Field(max_length=1000)
    size_bytes: int
    content_type: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_file(self) -> "DeclaredFile":
        name = clean_file_name(self.name)
        if not name:
            raise ValueError("The video file name cannot be empty.")
        check_video_size(self.size_bytes)
        self.content_type = resolve_video_content_type(name, self.content_type)
        self.name = name
        return self


class PartUrl(BaseModel):
    part_number: int
    size_bytes: int
    url: str


class MediaUploadInfo(BaseModel):
    asset_id: uuid.UUID
    status: MediaAssetStatus
    size_bytes: int
    part_size_bytes: int
    total_parts: int
    max_parallel_parts: int = settings.media_client_parallel_parts
    upload_deadline: Optional[datetime] = None
    uploaded_part_numbers: list[int] = Field(default_factory=list)
    parts: list[PartUrl] = Field(default_factory=list)


class MediaStateResponse(BaseModel):
    job_id: uuid.UUID
    media_status: Optional[MediaAssetStatus] = None
    media_expires_at: Optional[datetime] = None
    upload: Optional[MediaUploadInfo] = None
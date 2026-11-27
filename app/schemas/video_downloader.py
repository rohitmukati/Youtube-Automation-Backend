# app/schemas/video_downloader.py
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class VideoInfoRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.strip()


class DownloadOption(BaseModel):
    id: str
    kind: Literal["video", "audio"]
    label: str
    ext: str
    quality: int
    has_audio: bool
    size_bytes: Optional[int] = None
    needs_processing: bool = False


class VideoInfoResponse(BaseModel):
    platform: Literal["youtube", "instagram"]
    source_url: str
    title: str
    uploader: Optional[str] = None
    duration_seconds: Optional[int] = None
    thumbnail_url: Optional[str] = None
    options: list[DownloadOption]


class PrepareDownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    option_id: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9]+$")

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.strip()


class PrepareDownloadResponse(BaseModel):
    token: str
    filename: str
    expires_in: int
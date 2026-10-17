import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.models.storage_bucket import StorageFileType
from app.utils.tags import normalize_tags


class UpdateTagsRequest(BaseModel):
    tags: list[str] = Field(max_length=100)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return normalize_tags(value)


class StorageFileResponse(BaseModel):
    id: uuid.UUID
    file_type: StorageFileType
    original_name: str
    content_type: str
    size_bytes: int
    tags: list[str]
    signed_url: Optional[str] = None
    created_at: datetime


class StorageFileListResponse(BaseModel):
    files: list[StorageFileResponse]
    total: int
    limit: int
    offset: int


class MessageResponse(BaseModel):
    message: str
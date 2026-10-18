# app/schemas/yt_management.py
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings


# ── Request Schemas ───────────────────────────────────────────────────

class ConnectAccountRequest(BaseModel):
    """Before connecting a YouTube account, the user provides only the nickname."""
    nickname: str = Field(
        min_length=1,
        max_length=100,
        description="Give your YouTube account a name, e.g. 'Gaming Channel'",
    )


class UpdateAccountRequest(BaseModel):
    """Updates the connected account's nickname and/or notes. Empty notes are removed when sent."""
    nickname: Optional[str] = Field(default=None, min_length=1, max_length=100)
    notes: Optional[str] = None

    @field_validator("nickname")
    @classmethod
    def clean_nickname(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("The nickname cannot be empty.")
        return cleaned

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.replace("\r\n", "\n").strip()
        if len(cleaned) > settings.yt_account_notes_max_length:
            raise ValueError(
                f"Notes can be at most {settings.yt_account_notes_max_length} characters long."
            )
        return cleaned or None

    @model_validator(mode="after")
    def require_a_change(self) -> "UpdateAccountRequest":
        if self.nickname is None and "notes" not in self.model_fields_set:
            raise ValueError("Send a nickname, notes, or both.")
        return self


# ── Response Schemas ──────────────────────────────────────────────────

class ConnectInitiateResponse(BaseModel):
    """
    The frontend redirects the user to this URL.
    The Google consent screen opens.
    """
    auth_url: str
    message: str = "Visit this URL to connect your YouTube account."


class YouTubeAccountResponse(BaseModel):
    """Public information for the connected YouTube account."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nickname: str
    channel_id: str
    channel_title: str
    channel_thumbnail_url: Optional[str] = None
    google_email: Optional[str] = None
    notes: Optional[str] = None
    is_active: bool
    needs_reconnect: bool = False
    last_used_at: Optional[datetime] = None
    created_at: datetime


class AccountListResponse(BaseModel):
    """All connected accounts for the user."""
    accounts: list[YouTubeAccountResponse]
    total: int


class ConnectSuccessResponse(BaseModel):
    """Success response after the OAuth callback."""
    message: str
    account: YouTubeAccountResponse


class MessageResponse(BaseModel):
    message: str
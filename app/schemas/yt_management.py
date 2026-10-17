import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Request Schemas ───────────────────────────────────────────────────

class ConnectAccountRequest(BaseModel):
    """User YouTube account connect karne se pehle sirf nickname deta hai."""
    nickname: str = Field(
        min_length=1,
        max_length=100,
        description="Apne YouTube account ko ek naam do, e.g. 'Gaming Channel'",
    )


class RenameAccountRequest(BaseModel):
    """Already connected account ka nickname badalna."""
    nickname: str = Field(min_length=1, max_length=100)


# ── Response Schemas ──────────────────────────────────────────────────

class ConnectInitiateResponse(BaseModel):
    """
    Frontend is URL par user ko redirect karega.
    Google consent screen khulega.
    """
    auth_url: str
    message: str = "Is URL par jaake apna YouTube account connect karo."


class YouTubeAccountResponse(BaseModel):
    """Connected YouTube account ki public info."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nickname: str
    channel_id: str
    channel_title: str
    channel_thumbnail_url: Optional[str] = None
    is_active: bool
    needs_reconnect: bool = False
    last_used_at: Optional[datetime] = None
    created_at: datetime


class AccountListResponse(BaseModel):
    """User ke saare connected accounts."""
    accounts: list[YouTubeAccountResponse]
    total: int


class ConnectSuccessResponse(BaseModel):
    """OAuth callback ke baad success response."""
    message: str
    account: YouTubeAccountResponse


class MessageResponse(BaseModel):
    message: str
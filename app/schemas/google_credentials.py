# app/schemas/google_credentials.py
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.google_credentials import clean_client_id, clean_client_secret


class SaveGoogleCredentialsRequest(BaseModel):
    client_id: str = Field(max_length=255)
    client_secret: str = Field(max_length=512, repr=False)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        return clean_client_id(value)

    @field_validator("client_secret")
    @classmethod
    def validate_client_secret(cls, value: str) -> str:
        return clean_client_secret(value)


class GoogleCredentialsResponse(BaseModel):
    configured: bool
    client_id: Optional[str] = None
    client_secret_saved: bool = False
    redirect_uri: str
    updated_at: Optional[datetime] = None


class MessageResponse(BaseModel):
    message: str

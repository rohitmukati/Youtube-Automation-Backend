import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.utils.validators import (
    normalize_email,
    normalize_name,
    normalize_phone,
    validate_otp_format,
    validate_password_strength,
)


class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(max_length=128)
    phone: Optional[str] = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("password")
    @classmethod
    def check_password(cls, value: str) -> str:
        return validate_password_strength(value)

    @field_validator("phone")
    @classmethod
    def clean_phone(cls, value: Optional[str]) -> Optional[str]:
        return normalize_phone(value)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str
    new_password: str = Field(max_length=128)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("otp")
    @classmethod
    def check_otp(cls, value: str) -> str:
        return validate_otp_format(value)

    @field_validator("new_password")
    @classmethod
    def check_password(cls, value: str) -> str:
        return validate_password_strength(value)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    phone: Optional[str] = None
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthResponse(BaseModel):
    user: UserResponse
    tokens: TokenResponse


class MessageResponse(BaseModel):
    message: str

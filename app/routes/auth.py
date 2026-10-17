from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.config import settings
from app.core.dependencies import DbSession, get_email_service
from app.schemas.auth import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RefreshTokenRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
)
from app.service.auth_service import AuthService
from app.service.email_service import EmailService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, session: DbSession) -> AuthResponse:
    return await AuthService(session).signup(payload)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, session: DbSession) -> AuthResponse:
    return await AuthService(session).login(payload)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshTokenRequest, session: DbSession) -> TokenResponse:
    return await AuthService(session).refresh(payload.refresh_token)


@router.post("/logout", response_model=MessageResponse)
async def logout(payload: RefreshTokenRequest, session: DbSession) -> MessageResponse:
    await AuthService(session).logout(payload.refresh_token)
    return MessageResponse(message="You have been logged out successfully.")


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    session: DbSession,
    email_service: Annotated[EmailService, Depends(get_email_service)],
) -> MessageResponse:
    delivery = await AuthService(session).request_password_reset(payload.email)
    if delivery is not None:
        background_tasks.add_task(email_service.send_password_reset_otp, delivery)
    return MessageResponse(
        message=(
            "If an account exists for this email, a verification code has been sent. "
            f"The code is valid for {settings.password_reset_otp_expire_minutes} minutes."
        )
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(payload: ResetPasswordRequest, session: DbSession) -> MessageResponse:
    await AuthService(session).reset_password(payload)
    return MessageResponse(
        message="Your password has been reset successfully. Please log in with your new password."
    )

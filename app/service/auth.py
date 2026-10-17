import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import security
from app.core.exceptions import (
    AccountDisabledError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidOtpError,
    InvalidTokenError,
    TokenExpiredError,
    TooManyAttemptsError,
)
from app.crud import auth as auth_crud
from app.models.auth import User
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
)
from app.service.email import OtpDelivery
from app.utils.datetime_utils import ensure_utc, utc_now

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def signup(self, data: SignupRequest) -> AuthResponse:
        if await auth_crud.get_user_by_email(self._session, data.email):
            raise EmailAlreadyRegisteredError()

        password_hash = await security.hash_password(data.password)
        try:
            user = await auth_crud.create_user(
                self._session,
                name=data.name,
                email=data.email,
                phone=data.phone,
                password_hash=password_hash,
            )
            tokens = await self._issue_tokens(user)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise EmailAlreadyRegisteredError() from exc

        logger.info("New user registered: %s", user.id)
        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)

    async def login(self, data: LoginRequest) -> AuthResponse:
        user = await auth_crud.get_user_by_email(self._session, data.email)
        if user is None:
            await security.burn_password_check(data.password)
            raise InvalidCredentialsError()

        if not await security.verify_password(data.password, user.password_hash):
            raise InvalidCredentialsError()

        if not user.is_active:
            raise AccountDisabledError()

        if security.password_needs_rehash(user.password_hash):
            user.password_hash = await security.hash_password(data.password)

        tokens = await self._issue_tokens(user)
        await self._session.commit()
        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)

    async def refresh(self, raw_refresh_token: str) -> TokenResponse:
        record = await auth_crud.get_refresh_token_by_hash(
            self._session, security.hash_token(raw_refresh_token)
        )
        if record is None:
            raise InvalidTokenError()

        if record.revoked_at is not None:
            await auth_crud.revoke_all_refresh_tokens(self._session, record.user_id)
            await self._session.commit()
            logger.warning("Revoked refresh token reuse detected for user %s", record.user_id)
            raise InvalidTokenError()

        if ensure_utc(record.expires_at) <= utc_now():
            raise TokenExpiredError()

        user = await auth_crud.get_user_by_id(self._session, record.user_id)
        if user is None:
            raise InvalidTokenError()
        if not user.is_active:
            raise AccountDisabledError()

        record.revoked_at = utc_now()
        tokens = await self._issue_tokens(user)
        await self._session.commit()
        return tokens

    async def logout(self, raw_refresh_token: str) -> None:
        record = await auth_crud.get_refresh_token_by_hash(
            self._session, security.hash_token(raw_refresh_token)
        )
        if record is not None and record.revoked_at is None:
            record.revoked_at = utc_now()
            await self._session.commit()

    async def request_password_reset(self, email: str) -> Optional[OtpDelivery]:
        user = await auth_crud.get_user_by_email(self._session, email)
        if user is None or not user.is_active:
            return None

        latest = await auth_crud.get_latest_password_reset_otp(self._session, user.id)
        cooldown = timedelta(seconds=settings.password_reset_otp_resend_cooldown_seconds)
        if latest is not None and ensure_utc(latest.created_at) + cooldown > utc_now():
            return None

        await auth_crud.invalidate_password_reset_otps(self._session, user.id)
        otp = security.generate_otp()
        await auth_crud.create_password_reset_otp(
            self._session,
            user_id=user.id,
            otp_hash=security.hash_otp(user.email, otp),
            expires_at=utc_now() + timedelta(minutes=settings.password_reset_otp_expire_minutes),
        )
        await self._session.commit()
        return OtpDelivery(
            email=user.email,
            name=user.name,
            otp=otp,
            expires_in_minutes=settings.password_reset_otp_expire_minutes,
        )

    async def reset_password(self, data: ResetPasswordRequest) -> None:
        user = await auth_crud.get_user_by_email(self._session, data.email)
        if user is None or not user.is_active:
            raise InvalidOtpError()

        record = await auth_crud.get_active_password_reset_otp(self._session, user.id)
        if record is None or ensure_utc(record.expires_at) <= utc_now():
            raise InvalidOtpError()

        if record.attempts >= settings.password_reset_otp_max_attempts:
            raise TooManyAttemptsError()

        if not security.verify_otp(user.email, data.otp, record.otp_hash):
            record.attempts += 1
            await self._session.commit()
            raise InvalidOtpError()

        user.password_hash = await security.hash_password(data.new_password)
        record.used_at = utc_now()
        await auth_crud.revoke_all_refresh_tokens(self._session, user.id)
        await self._session.commit()
        logger.info("Password reset completed for user %s", user.id)

    async def _issue_tokens(self, user: User) -> TokenResponse:
        access_token, expires_in = security.create_access_token(user.id)
        raw_refresh_token = security.generate_refresh_token()
        await auth_crud.create_refresh_token(
            self._session,
            user_id=user.id,
            token_hash=security.hash_token(raw_refresh_token),
            expires_at=utc_now() + timedelta(days=settings.refresh_token_expire_days),
        )
        return TokenResponse(
            access_token=access_token,
            refresh_token=raw_refresh_token,
            expires_in=expires_in,
        )

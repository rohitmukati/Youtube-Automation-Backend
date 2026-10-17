import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import PasswordResetOtp, RefreshToken, User
from app.utils.datetime_utils import utc_now


async def get_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> Optional[User]:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    phone: Optional[str],
    password_hash: str,
) -> User:
    user = User(name=name, email=email, phone=phone, password_hash=password_hash)
    session.add(user)
    await session.flush()
    return user


async def create_refresh_token(
    session: AsyncSession, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime
) -> RefreshToken:
    record = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    session.add(record)
    await session.flush()
    return record


async def get_refresh_token_by_hash(session: AsyncSession, token_hash: str) -> Optional[RefreshToken]:
    result = await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    return result.scalar_one_or_none()


async def revoke_all_refresh_tokens(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )


async def create_password_reset_otp(
    session: AsyncSession, *, user_id: uuid.UUID, otp_hash: str, expires_at: datetime
) -> PasswordResetOtp:
    record = PasswordResetOtp(user_id=user_id, otp_hash=otp_hash, expires_at=expires_at)
    session.add(record)
    await session.flush()
    return record


async def get_latest_password_reset_otp(
    session: AsyncSession, user_id: uuid.UUID
) -> Optional[PasswordResetOtp]:
    result = await session.execute(
        select(PasswordResetOtp)
        .where(PasswordResetOtp.user_id == user_id)
        .order_by(PasswordResetOtp.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_password_reset_otp(
    session: AsyncSession, user_id: uuid.UUID
) -> Optional[PasswordResetOtp]:
    result = await session.execute(
        select(PasswordResetOtp)
        .where(PasswordResetOtp.user_id == user_id, PasswordResetOtp.used_at.is_(None))
        .order_by(PasswordResetOtp.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def invalidate_password_reset_otps(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(PasswordResetOtp)
        .where(PasswordResetOtp.user_id == user_id, PasswordResetOtp.used_at.is_(None))
        .values(used_at=utc_now())
    )

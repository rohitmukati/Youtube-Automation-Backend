import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.password_reset_otp import PasswordResetOtp
from app.utils.datetime_utils import utc_now


async def create(
    session: AsyncSession, *, user_id: uuid.UUID, otp_hash: str, expires_at: datetime
) -> PasswordResetOtp:
    record = PasswordResetOtp(user_id=user_id, otp_hash=otp_hash, expires_at=expires_at)
    session.add(record)
    await session.flush()
    return record


async def get_latest_for_user(session: AsyncSession, user_id: uuid.UUID) -> Optional[PasswordResetOtp]:
    result = await session.execute(
        select(PasswordResetOtp)
        .where(PasswordResetOtp.user_id == user_id)
        .order_by(PasswordResetOtp.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_for_user(session: AsyncSession, user_id: uuid.UUID) -> Optional[PasswordResetOtp]:
    result = await session.execute(
        select(PasswordResetOtp)
        .where(PasswordResetOtp.user_id == user_id, PasswordResetOtp.used_at.is_(None))
        .order_by(PasswordResetOtp.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def invalidate_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(PasswordResetOtp)
        .where(PasswordResetOtp.user_id == user_id, PasswordResetOtp.used_at.is_(None))
        .values(used_at=utc_now())
    )

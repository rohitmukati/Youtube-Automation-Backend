import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken
from app.utils.datetime_utils import utc_now


async def create(
    session: AsyncSession, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime
) -> RefreshToken:
    record = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    session.add(record)
    await session.flush()
    return record


async def get_by_hash(session: AsyncSession, token_hash: str) -> Optional[RefreshToken]:
    result = await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    return result.scalar_one_or_none()


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )

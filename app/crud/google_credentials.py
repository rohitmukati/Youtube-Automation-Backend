# app/crud/google_credentials.py
import uuid
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.google_credentials import GoogleCredential
from app.models.yt_management import YouTubeAccount
from app.utils.datetime_utils import utc_now


async def get_by_user(session: AsyncSession, user_id: uuid.UUID) -> Optional[GoogleCredential]:
    result = await session.execute(
        select(GoogleCredential)
        .where(GoogleCredential.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    client_id: str,
    encrypted_client_secret: str,
) -> GoogleCredential:
    record = GoogleCredential(
        user_id=user_id,
        client_id=client_id,
        encrypted_client_secret=encrypted_client_secret,
    )
    session.add(record)
    await session.flush()
    return record


async def update_record(
    session: AsyncSession,
    record: GoogleCredential,
    *,
    client_id: str,
    encrypted_client_secret: str,
) -> GoogleCredential:
    record.client_id = client_id
    record.encrypted_client_secret = encrypted_client_secret
    record.updated_at = utc_now()
    await session.flush()
    return record


async def delete(session: AsyncSession, record: GoogleCredential) -> None:
    await session.delete(record)
    await session.flush()


async def flag_user_accounts_need_reconnect(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        update(YouTubeAccount)
        .where(YouTubeAccount.user_id == user_id, YouTubeAccount.is_active.is_(True))
        .values(needs_reconnect=True)
    )
    return result.rowcount or 0

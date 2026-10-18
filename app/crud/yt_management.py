# app/crud/yt_management.py
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.yt_management import OAuthState, YouTubeAccount
from app.utils.datetime_utils import utc_now


# ── OAuthState CRUD ───────────────────────────────────────────────────

async def create_oauth_state(
    session: AsyncSession,
    *,
    state_token: str,
    user_id: uuid.UUID,
    nickname: str,
    expires_at: datetime,
) -> OAuthState:
    record = OAuthState(
        state_token=state_token,
        user_id=user_id,
        nickname=nickname,
        expires_at=expires_at,
    )
    session.add(record)
    await session.flush()
    return record


async def get_oauth_state(
    session: AsyncSession, state_token: str
) -> Optional[OAuthState]:
    result = await session.execute(
        select(OAuthState).where(OAuthState.state_token == state_token)
    )
    return result.scalar_one_or_none()


async def delete_oauth_state(session: AsyncSession, state_token: str) -> None:
    await session.execute(
        delete(OAuthState).where(OAuthState.state_token == state_token)
    )


async def cleanup_expired_oauth_states(session: AsyncSession) -> None:
    """Purane expire ho chuke states delete karo. Startup ya periodic task mein call karo."""
    await session.execute(
        delete(OAuthState).where(OAuthState.expires_at <= utc_now())
    )


# ── YouTubeAccount CRUD ───────────────────────────────────────────────

async def create_youtube_account(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    nickname: str,
    channel_id: str,
    channel_title: str,
    channel_thumbnail_url: Optional[str],
    encrypted_access_token: str,
    encrypted_refresh_token: str,
    token_expires_at: datetime,
    scopes: str,
    google_email: Optional[str] = None,
) -> YouTubeAccount:
    account = YouTubeAccount(
        user_id=user_id,
        nickname=nickname,
        channel_id=channel_id,
        channel_title=channel_title,
        channel_thumbnail_url=channel_thumbnail_url,
        google_email=google_email,
        encrypted_access_token=encrypted_access_token,
        encrypted_refresh_token=encrypted_refresh_token,
        token_expires_at=token_expires_at,
        scopes=scopes,
    )
    session.add(account)
    await session.flush()
    return account


async def get_accounts_by_user(
    session: AsyncSession, user_id: uuid.UUID
) -> list[YouTubeAccount]:
    result = await session.execute(
        select(YouTubeAccount)
        .where(YouTubeAccount.user_id == user_id)
        .order_by(YouTubeAccount.created_at.asc())
    )
    return list(result.scalars().all())


async def get_account_by_id(
    session: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[YouTubeAccount]:
    """Sirf us user ka account return karo — dusre user ke accounts nahi."""
    result = await session.execute(
        select(YouTubeAccount).where(
            YouTubeAccount.id == account_id,
            YouTubeAccount.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_account_by_channel_id(
    session: AsyncSession, user_id: uuid.UUID, channel_id: str
) -> Optional[YouTubeAccount]:
    """Check karo ki same channel already connected hai ya nahi."""
    result = await session.execute(
        select(YouTubeAccount).where(
            YouTubeAccount.user_id == user_id,
            YouTubeAccount.channel_id == channel_id,
        )
    )
    return result.scalar_one_or_none()


async def update_account_tokens(
    session: AsyncSession,
    account: YouTubeAccount,
    *,
    encrypted_access_token: str,
    token_expires_at: datetime,
) -> YouTubeAccount:
    """Access token refresh hone par update karo."""
    account.encrypted_access_token = encrypted_access_token
    account.token_expires_at = token_expires_at
    account.last_used_at = utc_now()
    await session.flush()
    return account


async def reconnect_account(
    session: AsyncSession,
    account: YouTubeAccount,
    *,
    nickname: str,
    channel_title: str,
    channel_thumbnail_url: Optional[str],
    encrypted_access_token: str,
    encrypted_refresh_token: str,
    token_expires_at: datetime,
    scopes: str,
    google_email: Optional[str] = None,
) -> YouTubeAccount:
    account.nickname = nickname
    account.channel_title = channel_title
    account.channel_thumbnail_url = channel_thumbnail_url
    if google_email:
        account.google_email = google_email
    account.encrypted_access_token = encrypted_access_token
    account.encrypted_refresh_token = encrypted_refresh_token
    account.token_expires_at = token_expires_at
    account.scopes = scopes
    account.is_active = True
    account.needs_reconnect = False
    await session.flush()
    return account


async def mark_needs_reconnect(
    session: AsyncSession, account: YouTubeAccount
) -> None:
    account.needs_reconnect = True
    await session.flush()


async def update_account_nickname(
    session: AsyncSession,
    account: YouTubeAccount,
    nickname: str,
) -> YouTubeAccount:
    account.nickname = nickname
    await session.flush()
    return account


async def update_account_details(
    session: AsyncSession,
    account: YouTubeAccount,
    *,
    nickname: Optional[str],
    notes: Optional[str],
    update_notes: bool,
) -> YouTubeAccount:
    if nickname is not None:
        account.nickname = nickname
    if update_notes:
        account.notes = notes
    await session.flush()
    return account


async def update_account_profile(
    session: AsyncSession,
    account: YouTubeAccount,
    *,
    google_email: Optional[str],
    channel_title: Optional[str],
    channel_thumbnail_url: Optional[str],
) -> YouTubeAccount:
    if google_email:
        account.google_email = google_email
    if channel_title:
        account.channel_title = channel_title
    if channel_thumbnail_url:
        account.channel_thumbnail_url = channel_thumbnail_url
    await session.flush()
    return account


async def deactivate_account(
    session: AsyncSession, account: YouTubeAccount
) -> None:
    """
    Disconnect = tokens clear karo + is_active = False.
    Row rakhte hain audit ke liye; hard delete nahi karte.
    """
    account.is_active = False
    account.needs_reconnect = False
    account.encrypted_access_token = ""
    account.encrypted_refresh_token = ""
    await session.flush()
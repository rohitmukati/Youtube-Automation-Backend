# app/models/yt_management.py
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.datetime_utils import utc_now


class YouTubeAccount(TimestampMixin, Base):
    """
    Ek user ke connected YouTube accounts store karta hai.
    Ek user ke paas unlimited accounts ho sakte hain.
    OAuth tokens encrypted form mein store hote hain.
    """
    __tablename__ = "youtube_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "channel_id", name="uq_youtube_accounts_user_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # User-given name, e.g. "My Gaming Channel", "Business Account"
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)

    # Fetched from YouTube API after OAuth success
    channel_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    channel_title: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_thumbnail_url: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )

    # Google account email that owns this channel (from OpenID userinfo)
    google_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    # Free text note written by the user, up to 500 characters
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Encrypted with Fernet (TOKEN_ENCRYPTION_KEY)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Space-separated scopes granted
    scopes: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    needs_reconnect: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OAuthState(Base):
    """
    OAuth flow ke dauran state token temporarily store karta hai.
    Callback par verify hone ke baad delete ho jaata hai.
    Expires_at se stale states auto-cleanup ho jaate hain.
    """
    __tablename__ = "oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    state_token: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
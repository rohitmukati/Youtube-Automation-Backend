# app/models/yt_analytics.py
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.datetime_utils import utc_now


class YtAnalyticsSync(TimestampMixin, Base):
    __tablename__ = "yt_analytics_syncs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("youtube_accounts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    videos_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class YtChannelSnapshot(TimestampMixin, Base):
    __tablename__ = "yt_channel_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_date", name="uq_yt_channel_snapshots_account_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("youtube_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    subscribers: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    hidden_subscriber_count: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_views: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    video_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class YtVideo(TimestampMixin, Base):
    __tablename__ = "yt_videos"
    __table_args__ = (
        UniqueConstraint("account_id", "video_id", name="uq_yt_videos_account_video"),
        Index("ix_yt_videos_account_published", "account_id", "published_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("youtube_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    video_id: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    category_id: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    default_language: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_short: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    privacy_status: Mapped[str] = mapped_column(String(20), nullable=False, default="public")
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    view_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    like_count: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    comment_count: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    engagement_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    performance_index: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stats_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class YtAnalyticsCache(Base):
    __tablename__ = "yt_analytics_cache"
    __table_args__ = (
        UniqueConstraint("account_id", "cache_key", name="uq_yt_analytics_cache_account_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("youtube_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    cache_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
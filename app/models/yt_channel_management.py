# app/models/yt_channel_management.py
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.utils.datetime_utils import utc_now
from app.utils.yt_channel_management import (
    ThumbnailStatus,
    UploadJobStatus,
    UploadTargetStatus,
)


class YtUploadJob(TimestampMixin, Base):
    __tablename__ = "yt_upload_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    storage_file_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid,
        ForeignKey("storage_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    thumbnail_file_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid,
        ForeignKey("storage_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    thumbnail_file_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    category_id: Mapped[str] = mapped_column(String(10), nullable=False)
    privacy_status: Mapped[str] = mapped_column(String(20), nullable=False)
    publish_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    made_for_kids: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_language: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    notify_subscribers: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    status: Mapped[str] = mapped_column(
        String(30), index=True, nullable=False, default=UploadJobStatus.QUEUED.value
    )

    targets: Mapped[list["YtUploadTarget"]] = relationship(
        "YtUploadTarget",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="YtUploadTarget.created_at, YtUploadTarget.id",
    )


class YtUploadTarget(TimestampMixin, Base):
    __tablename__ = "yt_upload_targets"
    __table_args__ = (
        UniqueConstraint("job_id", "channel_id", name="uq_yt_upload_targets_job_channel"),
        Index("ix_yt_upload_targets_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("yt_upload_jobs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid,
        ForeignKey("youtube_accounts.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_title: Mapped[str] = mapped_column(String(255), nullable=False)
    account_nickname: Mapped[str] = mapped_column(String(100), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=UploadTargetStatus.PENDING.value
    )
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    youtube_video_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    youtube_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    thumbnail_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ThumbnailStatus.NONE.value
    )
    thumbnail_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class YtUploadQuotaDay(Base):
    __tablename__ = "yt_upload_quota_days"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
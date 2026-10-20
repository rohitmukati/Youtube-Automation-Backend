# app/models/yt_ai_insights.py
import uuid
from typing import Optional

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class YtAiReport(TimestampMixin, Base):
    __tablename__ = "yt_ai_reports"
    __table_args__ = (
        Index("ix_yt_ai_reports_user_hash", "user_id", "input_hash"),
        Index("ix_yt_ai_reports_user_account_created", "user_id", "account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid,
        ForeignKey("youtube_accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    video_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    report_type: Mapped[str] = mapped_column(String(40), nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=28)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    data_quality: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
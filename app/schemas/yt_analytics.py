# app/schemas/yt_analytics.py
import uuid
from datetime import date, datetime
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, computed_field

from app.config import settings
from app.utils.yt_analytics import SyncStatus

T = TypeVar("T")


class ReportSection(BaseModel, Generic[T]):
    available: bool = True
    message: Optional[str] = None
    rows: list[T] = []


class SyncStatusResponse(BaseModel):
    account_id: uuid.UUID
    status: SyncStatus
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
    videos_synced: int = 0
    error: Optional[str] = None
    is_stale: bool = True


class ChannelGrowth(BaseModel):
    subscribers_7d: Optional[int] = None
    subscribers_28d: Optional[int] = None
    subscribers_90d: Optional[int] = None
    views_7d: Optional[int] = None
    views_28d: Optional[int] = None
    views_90d: Optional[int] = None


class VideoCounts(BaseModel):
    total: int = 0
    public: int = 0
    unlisted: int = 0
    private: int = 0
    shorts: int = 0
    long_form: int = 0


class VideoTotals(BaseModel):
    views: int = 0
    likes: int = 0
    comments: int = 0
    median_views: Optional[int] = None
    average_engagement_rate: Optional[float] = None


class ChannelOverviewResponse(BaseModel):
    account_id: uuid.UUID
    nickname: str
    channel_id: str
    channel_title: str
    channel_thumbnail_url: Optional[str] = None
    google_email: Optional[str] = None
    channel_url: str
    subscribers: Optional[int] = None
    hidden_subscriber_count: bool = False
    total_views: int = 0
    channel_video_count: int = 0
    snapshot_date: Optional[date] = None
    growth: ChannelGrowth
    videos: VideoCounts
    video_totals: VideoTotals
    analytics_enabled: bool
    needs_reconnect: bool
    has_data: bool
    sync: SyncStatusResponse


class MetricTotals(BaseModel):
    views: int = 0
    estimated_minutes_watched: int = 0
    watch_time_hours: float = 0.0
    average_view_duration_seconds: float = 0.0
    average_view_percentage: float = 0.0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    subscribers_gained: int = 0
    subscribers_lost: int = 0
    net_subscribers: int = 0
    engagement_rate: float = 0.0


class DailyPoint(BaseModel):
    date: str
    views: int = 0
    estimated_minutes_watched: int = 0
    average_view_duration_seconds: float = 0.0
    average_view_percentage: float = 0.0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    subscribers_gained: int = 0
    subscribers_lost: int = 0


class PerformanceResponse(BaseModel):
    account_id: uuid.UUID
    start_date: date
    end_date: date
    previous_start_date: date
    previous_end_date: date
    current: MetricTotals
    previous: MetricTotals
    change_percent: dict[str, Optional[float]]
    daily: list[DailyPoint]
    data_delay_days: int = settings.yt_analytics_data_delay_days
    fetched_at: datetime


class BreakdownRow(BaseModel):
    key: str
    views: int = 0
    estimated_minutes_watched: int = 0
    share_percent: float = 0.0


class DemographicRow(BaseModel):
    age_group: str
    gender: str
    viewer_percentage: float


class CountryRow(BaseModel):
    country: str
    views: int = 0
    estimated_minutes_watched: int = 0
    average_view_duration_seconds: float = 0.0
    subscribers_gained: int = 0
    share_percent: float = 0.0


class AudienceResponse(BaseModel):
    account_id: uuid.UUID
    start_date: date
    end_date: date
    demographics: ReportSection[DemographicRow]
    countries: ReportSection[CountryRow]
    devices: ReportSection[BreakdownRow]
    subscribed_status: ReportSection[BreakdownRow]
    fetched_at: datetime


class TrafficResponse(BaseModel):
    account_id: uuid.UUID
    start_date: date
    end_date: date
    sources: ReportSection[BreakdownRow]
    search_terms: ReportSection[BreakdownRow]
    content_types: ReportSection[BreakdownRow]
    fetched_at: datetime


class TopVideoRow(BaseModel):
    video_id: str
    title: str
    thumbnail_url: Optional[str] = None
    published_at: Optional[datetime] = None
    is_short: bool = False
    views: int = 0
    estimated_minutes_watched: int = 0
    average_view_duration_seconds: float = 0.0
    average_view_percentage: float = 0.0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    subscribers_gained: int = 0
    performance_index: Optional[float] = None

    @computed_field
    @property
    def youtube_url(self) -> str:
        return settings.yt_watch_url_template.format(video_id=self.video_id)


class TopVideosResponse(BaseModel):
    account_id: uuid.UUID
    start_date: date
    end_date: date
    videos: list[TopVideoRow]
    fetched_at: datetime


class VideoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    video_id: str
    title: str
    tags: list[str] = []
    category_id: Optional[str] = None
    default_language: Optional[str] = None
    published_at: datetime
    duration_seconds: int
    is_short: bool
    privacy_status: str
    thumbnail_url: Optional[str] = None
    view_count: int
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    engagement_rate: float
    performance_index: Optional[float] = None
    stats_synced_at: datetime

    @computed_field
    @property
    def youtube_url(self) -> str:
        return settings.yt_watch_url_template.format(video_id=self.video_id)


class VideoListResponse(BaseModel):
    videos: list[VideoResponse]
    total: int
    limit: int
    offset: int


class VideoComparison(BaseModel):
    rank_by_views: int
    total_videos: int
    channel_median_views: Optional[int] = None
    channel_average_engagement_rate: Optional[float] = None
    views_vs_median: Optional[float] = None
    engagement_vs_average: Optional[float] = None
    performance_label: str


class VideoDetailResponse(BaseModel):
    account_id: uuid.UUID
    video: VideoResponse
    description: str
    comparison: VideoComparison


class RetentionPoint(BaseModel):
    elapsed_ratio: float
    audience_watch_ratio: float
    relative_retention_performance: Optional[float] = None


class VideoAnalyticsResponse(BaseModel):
    account_id: uuid.UUID
    video_id: str
    start_date: date
    end_date: date
    totals: MetricTotals
    daily: ReportSection[DailyPoint]
    retention: ReportSection[RetentionPoint]
    traffic_sources: ReportSection[BreakdownRow]
    search_terms: ReportSection[BreakdownRow]
    demographics: ReportSection[DemographicRow]
    fetched_at: datetime


class CommentItem(BaseModel):
    text: str
    like_count: int = 0
    reply_count: int = 0
    published_at: Optional[str] = None


class VideoCommentsResponse(BaseModel):
    account_id: uuid.UUID
    video_id: str
    comments: list[CommentItem]
    comments_disabled: bool = False
    fetched_at: datetime


class PublishingInsightsResponse(BaseModel):
    account_id: uuid.UUID
    content_type: str
    timezone: str
    sample_size: int
    confidence: str
    best_weekday: Optional[str] = None
    best_hour: Optional[int] = None
    best_day_part: Optional[str] = None
    best_slots: list[dict]
    by_weekday: list[dict]
    by_hour: list[dict]
    by_day_part: list[dict]
    heatmap: list[dict]
    cadence: dict
    note: str


class ContentInsightsResponse(BaseModel):
    account_id: uuid.UUID
    content_type: str
    sample_size: int
    confidence: str
    best_videos: list[dict]
    worst_videos: list[dict]
    tags: dict
    titles: dict
    durations: list[dict]
    note: str


class PortfolioChannel(BaseModel):
    account_id: uuid.UUID
    nickname: str
    channel_title: str
    channel_thumbnail_url: Optional[str] = None
    subscribers: Optional[int] = None
    total_views: int = 0
    channel_video_count: int = 0
    synced_videos: int = 0
    recent_uploads_30d: int = 0
    subscribers_28d: Optional[int] = None
    views_28d: Optional[int] = None
    average_performance_index: Optional[float] = None
    last_published_at: Optional[datetime] = None
    top_video: Optional[dict] = None
    analytics_enabled: bool
    needs_reconnect: bool
    sync: SyncStatusResponse


class PortfolioOverviewResponse(BaseModel):
    total_channels: int
    total_subscribers: int
    total_views: int
    total_videos: int
    channels: list[PortfolioChannel]
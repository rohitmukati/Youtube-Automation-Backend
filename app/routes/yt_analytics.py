# app/routes/yt_analytics.py
import uuid
from datetime import date, datetime
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Path, Query, status

from app.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.schemas.yt_analytics import (
    AudienceResponse,
    ChannelOverviewResponse,
    ContentInsightsResponse,
    PerformanceResponse,
    PortfolioOverviewResponse,
    PublishingInsightsResponse,
    SyncStatusResponse,
    TopVideosResponse,
    TrafficResponse,
    VideoAnalyticsResponse,
    VideoCommentsResponse,
    VideoDetailResponse,
    VideoListResponse,
)
from app.service.yt_analytics import YtAnalyticsService, run_sync_job
from app.utils.yt_channel_management import PrivacyStatus
from app.utils.yt_analytics import ContentType, SortOrder, VideoSort

router = APIRouter(prefix="/yt-analytics", tags=["YouTube Analytics"])

Days = Annotated[
    Optional[int],
    Query(
        ge=1,
        le=settings.yt_analytics_max_days,
        description="Number of days ending at the latest available date. Default is 28.",
    ),
]
StartDate = Annotated[Optional[date], Query(description="Custom range start. Use with end_date.")]
EndDate = Annotated[Optional[date], Query(description="Custom range end. Use with start_date.")]
VideoId = Annotated[str, Path(min_length=5, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]


@router.get(
    "/overview",
    response_model=PortfolioOverviewResponse,
    summary="All connected channels side by side",
    description=(
        "Subscribers, views, upload activity and sync state for every connected channel. "
        "Uses the data already loaded, so it is fast and does not use YouTube quota."
    ),
)
async def get_portfolio_overview(
    current_user: CurrentUser,
    session: DbSession,
) -> PortfolioOverviewResponse:
    return await YtAnalyticsService(session).get_portfolio_overview(current_user.id)


@router.get(
    "/accounts/{account_id}/sync",
    response_model=SyncStatusResponse,
    summary="Data sync status of one channel",
)
async def get_sync_status(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> SyncStatusResponse:
    return await YtAnalyticsService(session).get_sync_status(current_user.id, account_id)


@router.post(
    "/accounts/{account_id}/sync",
    response_model=SyncStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Load or refresh the data of one channel",
    description=(
        "Loads the channel statistics and every video from YouTube in the background. "
        "Poll the sync status endpoint until it is completed. If a sync is already running, "
        "its current state is returned."
    ),
)
async def start_sync(
    account_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
) -> SyncStatusResponse:
    result, started = await YtAnalyticsService(session).start_sync(current_user.id, account_id)
    if started:
        background_tasks.add_task(run_sync_job, current_user.id, account_id)
    return result


@router.get(
    "/accounts/{account_id}/channel",
    response_model=ChannelOverviewResponse,
    summary="Channel summary for the dashboard header",
)
async def get_channel(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> ChannelOverviewResponse:
    return await YtAnalyticsService(session).get_channel(current_user.id, account_id)


@router.get(
    "/accounts/{account_id}/performance",
    response_model=PerformanceResponse,
    summary="Channel performance for a period compared with the period before",
)
async def get_performance(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
    days: Days = None,
    start_date: StartDate = None,
    end_date: EndDate = None,
) -> PerformanceResponse:
    return await YtAnalyticsService(session).get_performance(
        current_user.id, account_id, days=days, start_date=start_date, end_date=end_date
    )


@router.get(
    "/accounts/{account_id}/audience",
    response_model=AudienceResponse,
    summary="Who watches the channel",
)
async def get_audience(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
    days: Days = None,
    start_date: StartDate = None,
    end_date: EndDate = None,
) -> AudienceResponse:
    return await YtAnalyticsService(session).get_audience(
        current_user.id, account_id, days=days, start_date=start_date, end_date=end_date
    )


@router.get(
    "/accounts/{account_id}/traffic",
    response_model=TrafficResponse,
    summary="Where the views come from, including YouTube search terms",
)
async def get_traffic(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
    days: Days = None,
    start_date: StartDate = None,
    end_date: EndDate = None,
) -> TrafficResponse:
    return await YtAnalyticsService(session).get_traffic(
        current_user.id, account_id, days=days, start_date=start_date, end_date=end_date
    )


@router.get(
    "/accounts/{account_id}/top-videos",
    response_model=TopVideosResponse,
    summary="Best videos of a period",
)
async def get_top_videos(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
    days: Days = None,
    start_date: StartDate = None,
    end_date: EndDate = None,
    limit: Annotated[
        int, Query(ge=1, le=settings.yt_analytics_top_videos_max)
    ] = settings.yt_analytics_top_videos_default,
) -> TopVideosResponse:
    return await YtAnalyticsService(session).get_top_videos(
        current_user.id,
        account_id,
        days=days,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )


@router.get(
    "/accounts/{account_id}/videos",
    response_model=VideoListResponse,
    summary="Every video of the channel",
    description=(
        "Search, filter and sort the loaded videos. Sort by published date, views, likes, "
        "comments, engagement rate, performance index or duration."
    ),
)
async def list_videos(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
    search: Annotated[Optional[str], Query(max_length=100)] = None,
    content_type: Annotated[ContentType, Query()] = ContentType.ALL,
    privacy_status: Annotated[Optional[PrivacyStatus], Query()] = None,
    date_from: Annotated[Optional[datetime], Query()] = None,
    date_to: Annotated[Optional[datetime], Query()] = None,
    sort: Annotated[VideoSort, Query()] = VideoSort.PUBLISHED_AT,
    order: Annotated[SortOrder, Query()] = SortOrder.DESC,
    limit: Annotated[
        int, Query(ge=1, le=settings.yt_list_max_limit)
    ] = settings.yt_list_default_limit,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> VideoListResponse:
    return await YtAnalyticsService(session).list_videos(
        current_user.id,
        account_id,
        search=search,
        content_type=content_type,
        privacy_status=privacy_status.value if privacy_status else None,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/accounts/{account_id}/videos/{video_id}",
    response_model=VideoDetailResponse,
    summary="One video with a comparison against the rest of the channel",
)
async def get_video_detail(
    account_id: uuid.UUID,
    video_id: VideoId,
    current_user: CurrentUser,
    session: DbSession,
) -> VideoDetailResponse:
    return await YtAnalyticsService(session).get_video_detail(current_user.id, account_id, video_id)


@router.get(
    "/accounts/{account_id}/videos/{video_id}/analytics",
    response_model=VideoAnalyticsResponse,
    summary="Detailed analytics of one video",
    description=(
        "Daily performance, audience retention, traffic sources, search terms and "
        "demographics. Without a date range the whole life of the video is used."
    ),
)
async def get_video_analytics(
    account_id: uuid.UUID,
    video_id: VideoId,
    current_user: CurrentUser,
    session: DbSession,
    days: Days = None,
    start_date: StartDate = None,
    end_date: EndDate = None,
) -> VideoAnalyticsResponse:
    return await YtAnalyticsService(session).get_video_analytics(
        current_user.id,
        account_id,
        video_id,
        days=days,
        start_date=start_date,
        end_date=end_date,
    )


@router.get(
    "/accounts/{account_id}/videos/{video_id}/comments",
    response_model=VideoCommentsResponse,
    summary="Top comments of one video",
)
async def get_video_comments(
    account_id: uuid.UUID,
    video_id: VideoId,
    current_user: CurrentUser,
    session: DbSession,
) -> VideoCommentsResponse:
    return await YtAnalyticsService(session).get_video_comments(
        current_user.id, account_id, video_id
    )


@router.get(
    "/accounts/{account_id}/insights/publishing",
    response_model=PublishingInsightsResponse,
    summary="Best time to upload and upload habits",
    description=(
        "Compares every video with the videos published around the same time to find the "
        "weekdays and hours that work best. Results include a confidence level."
    ),
)
async def get_publishing_insights(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
    timezone: Annotated[Optional[str], Query(max_length=64)] = None,
    content_type: Annotated[ContentType, Query()] = ContentType.ALL,
) -> PublishingInsightsResponse:
    return await YtAnalyticsService(session).get_publishing_insights(
        current_user.id, account_id, timezone_name=timezone, content_type=content_type
    )


@router.get(
    "/accounts/{account_id}/insights/content",
    response_model=ContentInsightsResponse,
    summary="Best and weakest videos, tags, title patterns and duration",
)
async def get_content_insights(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
    content_type: Annotated[ContentType, Query()] = ContentType.ALL,
) -> ContentInsightsResponse:
    return await YtAnalyticsService(session).get_content_insights(
        current_user.id, account_id, content_type=content_type
    )
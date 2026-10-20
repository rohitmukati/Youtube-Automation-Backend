# app/service/yt_analytics.py
import asyncio
import logging
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from google.oauth2.credentials import Credentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import SessionFactory
from app.core.exceptions import (
    AccountDisabledError,
    AnalyticsApiDisabledError,
    AnalyticsScopeMissingError,
    AppException,
    CommentsPermissionMissingError,
    NotFoundError,
    ValidationError,
    YouTubeApiError,
    YouTubeQuotaError,
)
from app.crud import yt_analytics as analytics_crud
from app.crud import yt_management as yt_crud
from app.integrations import youtube_analytics_api as yt_api
from app.models.yt_analytics import YtAnalyticsSync, YtVideo
from app.models.yt_management import YouTubeAccount
from app.schemas.yt_analytics import (
    AudienceResponse,
    ChannelGrowth,
    ChannelOverviewResponse,
    CommentItem,
    ContentInsightsResponse,
    MetricTotals,
    PerformanceResponse,
    PortfolioChannel,
    PortfolioOverviewResponse,
    PublishingInsightsResponse,
    SyncStatusResponse,
    TopVideoRow,
    TopVideosResponse,
    TrafficResponse,
    VideoComparison,
    VideoCommentsResponse,
    VideoCounts,
    VideoDetailResponse,
    VideoAnalyticsResponse,
    VideoListResponse,
    VideoResponse,
    VideoTotals,
)
from app.service.yt_management import YouTubeManagementService
from app.utils.google_credentials import COMMENTS_SCOPE, is_permission_error
from app.utils.datetime_utils import ensure_utc, utc_now
from app.utils.yt_analytics import (
    ContentType,
    DateRange,
    SortOrder,
    SyncStatus,
    VideoPoint,
    VideoSort,
    breakdown_rows,
    build_cadence_insights,
    build_duration_insights,
    build_publishing_insights,
    build_tag_insights,
    build_title_insights,
    classify_youtube_error,
    clip,
    comments_disabled,
    compute_performance_indexes,
    confidence_for,
    daily_points,
    filter_points,
    number,
    performance_label,
    rank_videos,
    report_cache_key,
    report_rows,
    resolve_range,
    share_percent,
    split_daily_rows,
    summarize_daily,
    totals_change,
)

logger = logging.getLogger(__name__)

_RECONNECT_MESSAGE = "This YouTube account permission has expired. Please reconnect the account."
_FATAL_ERRORS = (YouTubeQuotaError, AnalyticsApiDisabledError, AnalyticsScopeMissingError)
_DAILY_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained,subscribersLost"
)
_TOP_VIDEO_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained"
)
_VIEW_METRICS = "views,estimatedMinutesWatched"
_SECTION_UNAVAILABLE = "YouTube did not return this report for the channel."


def _section(rows: list[Any], *, available: bool = True, message: Optional[str] = None) -> dict:
    return {"available": available, "message": message, "rows": rows}


def _to_point(video: YtVideo) -> VideoPoint:
    return VideoPoint(
        video_id=video.video_id,
        title=video.title,
        published_at=video.published_at,
        duration_seconds=video.duration_seconds,
        is_short=video.is_short,
        views=video.view_count,
        likes=video.like_count,
        comments=video.comment_count,
        engagement_rate=video.engagement_rate,
        performance_index=video.performance_index,
        tags=list(video.tags or []),
    )


def _has_analytics_scope(account: YouTubeAccount) -> bool:
    return settings.yt_analytics_scope in (account.scopes or "").split()


@dataclass
class ReportBatch:
    payloads: dict[str, Optional[dict]] = field(default_factory=dict)
    errors: dict[str, AppException] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)

    def section(self, key: str, mapper) -> dict:
        error = self.errors.get(key)
        if error is not None:
            return _section([], available=False, message=error.message)
        payload = self.payloads.get(key)
        if payload is None:
            return _section([], available=False, message=_SECTION_UNAVAILABLE)
        return _section(mapper(report_rows(payload)))

    def rows(self, key: str) -> list[dict[str, Any]]:
        return report_rows(self.payloads.get(key))

    def raise_if_failed(self) -> None:
        for error in self.errors.values():
            raise error


def _demographic_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "age_group": str(row.get("ageGroup", "")).replace("age", "", 1),
            "gender": str(row.get("gender", "")),
            "viewer_percentage": round(number(row.get("viewerPercentage")), 1),
        }
        for row in rows
    ]


def _country_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = sum(number(row.get("views")) for row in rows)
    return [
        {
            "country": str(row.get("country", "")),
            "views": int(number(row.get("views"))),
            "estimated_minutes_watched": int(number(row.get("estimatedMinutesWatched"))),
            "average_view_duration_seconds": round(number(row.get("averageViewDuration")), 1),
            "subscribers_gained": int(number(row.get("subscribersGained"))),
            "share_percent": share_percent(number(row.get("views")), total),
        }
        for row in rows
    ]


def _retention_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for row in rows:
        relative = row.get("relativeRetentionPerformance")
        points.append(
            {
                "elapsed_ratio": round(number(row.get("elapsedVideoTimeRatio")), 3),
                "audience_watch_ratio": round(number(row.get("audienceWatchRatio")), 3),
                "relative_retention_performance": round(number(relative), 3)
                if relative is not None
                else None,
            }
        )
    points.sort(key=lambda point: point["elapsed_ratio"])
    return points


class YtAnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_account(self, user_id: uuid.UUID, account_id: uuid.UUID) -> YouTubeAccount:
        account = await yt_crud.get_account_by_id(self._session, account_id, user_id)
        if account is None:
            raise NotFoundError("YouTube account not found.")
        if not account.is_active:
            raise AccountDisabledError(
                "This YouTube account has been disconnected. Please connect it again."
            )
        return account

    async def _get_analytics_account(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> YouTubeAccount:
        account = await self._get_account(user_id, account_id)
        if account.needs_reconnect:
            raise ValidationError(_RECONNECT_MESSAGE)
        if not _has_analytics_scope(account):
            raise AnalyticsScopeMissingError()
        return account

    async def _credentials(self, user_id: uuid.UUID, account: YouTubeAccount) -> Credentials:
        return await YouTubeManagementService(self._session).get_fresh_credentials(
            user_id, account.id
        )

    async def _call(self, function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except AppException:
            raise
        except Exception as exc:
            logger.warning("YouTube call %s failed: %s", function.__name__, exc)
            raise classify_youtube_error(exc) from exc

    async def _fetch_reports(
        self,
        user_id: uuid.UUID,
        account: YouTubeAccount,
        specs: dict[str, dict[str, Any]],
        *,
        range_for_query: DateRange,
    ) -> ReportBatch:
        batch = ReportBatch()
        missing: dict[str, dict[str, Any]] = {}
        fetched: list[datetime] = []

        for key, query in specs.items():
            cached = await analytics_crud.get_cache(self._session, account.id, key)
            if cached is not None:
                batch.payloads[key] = cached.payload
                fetched.append(ensure_utc(cached.fetched_at))
            else:
                missing[key] = query

        if missing:
            credentials = await self._credentials(user_id, account)

            async def run(key: str, query: dict[str, Any]):
                try:
                    payload = await self._call(
                        yt_api.query_report_sync,
                        credentials,
                        start_date=query.pop("start_date", range_for_query.start),
                        end_date=query.pop("end_date", range_for_query.end),
                        **query,
                    )
                    return key, payload, None
                except AppException as exc:
                    return key, None, exc

            outcomes = await asyncio.gather(*(run(key, dict(query)) for key, query in missing.items()))
            now = utc_now()
            for key, payload, error in outcomes:
                if error is not None:
                    batch.errors[key] = error
                    batch.payloads[key] = None
                    continue
                batch.payloads[key] = payload
                fetched.append(now)
                await analytics_crud.set_cache(
                    self._session,
                    account_id=account.id,
                    cache_key=key,
                    payload=payload,
                    ttl_seconds=settings.yt_analytics_report_ttl_seconds,
                )
            await self._session.commit()

        for error in batch.errors.values():
            if not isinstance(error, YouTubeApiError):
                raise error

        if fetched:
            batch.fetched_at = min(fetched)
        return batch

    def _sync_response(
        self, account_id: uuid.UUID, record: Optional[YtAnalyticsSync]
    ) -> SyncStatusResponse:
        if record is None:
            return SyncStatusResponse(account_id=account_id, status=SyncStatus.IDLE, is_stale=True)

        status = SyncStatus(record.status)
        error = record.last_error
        now = utc_now()
        if status is SyncStatus.RUNNING and record.started_at is not None:
            running_for = (now - ensure_utc(record.started_at)).total_seconds()
            if running_for > settings.yt_analytics_sync_stale_seconds:
                status = SyncStatus.FAILED
                error = "The last sync did not finish. Please start it again."

        last_synced = ensure_utc(record.last_synced_at) if record.last_synced_at else None
        is_stale = last_synced is None or (
            (now - last_synced).total_seconds() > settings.yt_analytics_sync_ttl_seconds
        )
        return SyncStatusResponse(
            account_id=account_id,
            status=status,
            started_at=record.started_at,
            finished_at=record.finished_at,
            last_synced_at=last_synced,
            videos_synced=record.videos_synced,
            error=error,
            is_stale=is_stale,
        )

    async def get_sync_status(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> SyncStatusResponse:
        account = await self._get_account(user_id, account_id)
        record = await analytics_crud.get_sync(self._session, account.id)
        return self._sync_response(account.id, record)

    async def start_sync(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> tuple[SyncStatusResponse, bool]:
        account = await self._get_account(user_id, account_id)
        if account.needs_reconnect:
            raise ValidationError(_RECONNECT_MESSAGE)

        stale_before = utc_now() - timedelta(seconds=settings.yt_analytics_sync_stale_seconds)
        claimed = await analytics_crud.claim_sync(
            self._session, account_id=account.id, user_id=user_id, stale_before=stale_before
        )
        await self._session.commit()
        record = await analytics_crud.get_sync(self._session, account.id)
        return self._sync_response(account.id, record), claimed

    async def execute_sync(self, user_id: uuid.UUID, account_id: uuid.UUID) -> None:
        error: Optional[str] = None
        videos_synced = 0
        try:
            videos_synced = await self._sync_channel(user_id, account_id)
        except AppException as exc:
            await self._session.rollback()
            error = exc.message
        except Exception:
            await self._session.rollback()
            logger.exception("Analytics sync failed for account %s", account_id)
            error = "Something went wrong while loading the channel data. Please try again."

        record = await analytics_crud.get_sync(self._session, account_id)
        if record is not None:
            await analytics_crud.finish_sync(
                self._session,
                record,
                succeeded=error is None,
                videos_synced=videos_synced,
                error=clip(error, settings.yt_analytics_sync_error_max_length),
            )
            await self._session.commit()

    async def _sync_channel(self, user_id: uuid.UUID, account_id: uuid.UUID) -> int:
        account = await self._get_account(user_id, account_id)
        credentials = await self._credentials(user_id, account)

        channel = await self._call(yt_api.fetch_channel_sync, credentials, account.channel_id)
        if not channel:
            raise NotFoundError("This channel could not be found on YouTube.")

        await analytics_crud.upsert_snapshot(
            self._session,
            account_id=account.id,
            snapshot_date=utc_now().date(),
            subscribers=channel["subscribers"],
            hidden_subscriber_count=channel["hidden_subscriber_count"],
            total_views=channel["total_views"],
            video_count=channel["video_count"],
        )
        await yt_crud.update_account_profile(
            self._session,
            account,
            google_email=None,
            channel_title=channel.get("title"),
            channel_thumbnail_url=channel.get("thumbnail_url"),
        )

        video_ids: list[str] = []
        truncated = False
        playlist_id = channel.get("uploads_playlist_id")
        if playlist_id:
            video_ids, truncated = await self._call(
                yt_api.list_upload_video_ids_sync,
                credentials,
                playlist_id,
                max_videos=settings.yt_analytics_max_videos,
            )

        items: list[dict[str, Any]] = []
        if video_ids:
            fetched = await self._call(yt_api.fetch_videos_sync, credentials, video_ids)
            items = [item for item in fetched if item["live_broadcast_content"] != "upcoming"]

        await analytics_crud.upsert_videos(
            self._session, account_id=account.id, user_id=user_id, items=items
        )
        if not truncated and (items or channel["video_count"] == 0):
            await analytics_crud.delete_videos_not_in(
                self._session, account.id, [item["video_id"] for item in items]
            )

        videos = await analytics_crud.get_all_videos(self._session, account.id)
        indexes: dict[str, Optional[float]] = {}
        for is_short in (True, False):
            group = [
                (video.video_id, video.published_at, video.view_count)
                for video in videos
                if video.is_short == is_short
            ]
            indexes.update(compute_performance_indexes(group))
        await analytics_crud.apply_performance_indexes(self._session, videos, indexes)
        await self._session.commit()

        logger.info(
            "Analytics sync finished for account %s with %s videos", account.id, len(items)
        )
        return len(items)

    async def _growth(self, account_id: uuid.UUID):
        latest = await analytics_crud.get_latest_snapshot(self._session, account_id)
        growth = ChannelGrowth()
        if latest is None:
            return latest, growth
        values: dict[str, Optional[int]] = {}
        for days in (7, 28, 90):
            earlier = await analytics_crud.get_snapshot_on_or_before(
                self._session, account_id, latest.snapshot_date - timedelta(days=days)
            )
            if earlier is None:
                values[f"subscribers_{days}d"] = None
                values[f"views_{days}d"] = None
                continue
            values[f"subscribers_{days}d"] = (
                latest.subscribers - earlier.subscribers
                if latest.subscribers is not None and earlier.subscribers is not None
                else None
            )
            values[f"views_{days}d"] = latest.total_views - earlier.total_views
        return latest, ChannelGrowth(**values)

    async def get_channel(self, user_id: uuid.UUID, account_id: uuid.UUID) -> ChannelOverviewResponse:
        account = await self._get_account(user_id, account_id)
        record = await analytics_crud.get_sync(self._session, account.id)
        videos = await analytics_crud.get_all_videos(self._session, account.id)
        latest, growth = await self._growth(account.id)

        counts = VideoCounts(
            total=len(videos),
            public=sum(1 for video in videos if video.privacy_status == "public"),
            unlisted=sum(1 for video in videos if video.privacy_status == "unlisted"),
            private=sum(1 for video in videos if video.privacy_status == "private"),
            shorts=sum(1 for video in videos if video.is_short),
            long_form=sum(1 for video in videos if not video.is_short),
        )
        views = [video.view_count for video in videos]
        rates = [video.engagement_rate for video in videos if video.view_count > 0]
        totals = VideoTotals(
            views=sum(views),
            likes=sum(video.like_count or 0 for video in videos),
            comments=sum(video.comment_count or 0 for video in videos),
            median_views=int(statistics.median(views)) if views else None,
            average_engagement_rate=round(statistics.fmean(rates), 2) if rates else None,
        )

        return ChannelOverviewResponse(
            account_id=account.id,
            nickname=account.nickname,
            channel_id=account.channel_id,
            channel_title=account.channel_title,
            channel_thumbnail_url=account.channel_thumbnail_url,
            google_email=account.google_email,
            channel_url=f"https://www.youtube.com/channel/{account.channel_id}",
            subscribers=latest.subscribers if latest else None,
            hidden_subscriber_count=latest.hidden_subscriber_count if latest else False,
            total_views=latest.total_views if latest else 0,
            channel_video_count=latest.video_count if latest else 0,
            snapshot_date=latest.snapshot_date if latest else None,
            growth=growth,
            videos=counts,
            video_totals=totals,
            analytics_enabled=_has_analytics_scope(account) and not account.needs_reconnect,
            needs_reconnect=account.needs_reconnect,
            has_data=record is not None and record.last_synced_at is not None,
            sync=self._sync_response(account.id, record),
        )

    async def get_performance(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        days: Optional[int],
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> PerformanceResponse:
        account = await self._get_analytics_account(user_id, account_id)
        current = resolve_range(days, start_date, end_date)
        previous = current.previous()
        combined = DateRange(start=previous.start, end=current.end)
        key = report_cache_key("daily", current.start, current.end)

        batch = await self._fetch_reports(
            user_id,
            account,
            {
                key: {
                    "metrics": _DAILY_METRICS,
                    "dimensions": "day",
                    "sort": "day",
                    "start_date": combined.start,
                    "end_date": combined.end,
                }
            },
            range_for_query=combined,
        )
        batch.raise_if_failed()

        current_rows, previous_rows = split_daily_rows(batch.rows(key), current)
        current_totals = summarize_daily(current_rows)
        previous_totals = summarize_daily(previous_rows)
        return PerformanceResponse(
            account_id=account.id,
            start_date=current.start,
            end_date=current.end,
            previous_start_date=previous.start,
            previous_end_date=previous.end,
            current=MetricTotals(**current_totals),
            previous=MetricTotals(**previous_totals),
            change_percent=totals_change(current_totals, previous_totals),
            daily=daily_points(current_rows),
            fetched_at=batch.fetched_at,
        )

    async def get_audience(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        days: Optional[int],
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> AudienceResponse:
        account = await self._get_analytics_account(user_id, account_id)
        rng = resolve_range(days, start_date, end_date)
        keys = {
            name: report_cache_key(name, rng.start, rng.end)
            for name in ("demographics", "countries", "devices", "subscribed")
        }
        batch = await self._fetch_reports(
            user_id,
            account,
            {
                keys["demographics"]: {
                    "metrics": "viewerPercentage",
                    "dimensions": "ageGroup,gender",
                    "sort": "gender,ageGroup",
                },
                keys["countries"]: {
                    "metrics": "views,estimatedMinutesWatched,averageViewDuration,subscribersGained",
                    "dimensions": "country",
                    "sort": "-views",
                    "max_results": settings.yt_analytics_country_limit,
                },
                keys["devices"]: {
                    "metrics": _VIEW_METRICS,
                    "dimensions": "deviceType",
                    "sort": "-views",
                },
                keys["subscribed"]: {
                    "metrics": _VIEW_METRICS,
                    "dimensions": "subscribedStatus",
                    "sort": "-views",
                },
            },
            range_for_query=rng,
        )
        return AudienceResponse(
            account_id=account.id,
            start_date=rng.start,
            end_date=rng.end,
            demographics=batch.section(keys["demographics"], _demographic_rows),
            countries=batch.section(keys["countries"], _country_rows),
            devices=batch.section(keys["devices"], lambda rows: breakdown_rows(rows, "deviceType")),
            subscribed_status=batch.section(
                keys["subscribed"], lambda rows: breakdown_rows(rows, "subscribedStatus")
            ),
            fetched_at=batch.fetched_at,
        )

    async def get_traffic(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        days: Optional[int],
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> TrafficResponse:
        account = await self._get_analytics_account(user_id, account_id)
        rng = resolve_range(days, start_date, end_date)
        keys = {
            name: report_cache_key(name, rng.start, rng.end)
            for name in ("sources", "search", "content")
        }
        batch = await self._fetch_reports(
            user_id,
            account,
            {
                keys["sources"]: {
                    "metrics": _VIEW_METRICS,
                    "dimensions": "insightTrafficSourceType",
                    "sort": "-views",
                },
                keys["search"]: {
                    "metrics": _VIEW_METRICS,
                    "dimensions": "insightTrafficSourceDetail",
                    "filters": "insightTrafficSourceType==YT_SEARCH",
                    "sort": "-views",
                    "max_results": settings.yt_analytics_search_terms_limit,
                },
                keys["content"]: {
                    "metrics": _VIEW_METRICS,
                    "dimensions": "creatorContentType",
                    "sort": "-views",
                },
            },
            range_for_query=rng,
        )
        return TrafficResponse(
            account_id=account.id,
            start_date=rng.start,
            end_date=rng.end,
            sources=batch.section(
                keys["sources"], lambda rows: breakdown_rows(rows, "insightTrafficSourceType")
            ),
            search_terms=batch.section(
                keys["search"], lambda rows: breakdown_rows(rows, "insightTrafficSourceDetail")
            ),
            content_types=batch.section(
                keys["content"], lambda rows: breakdown_rows(rows, "creatorContentType")
            ),
            fetched_at=batch.fetched_at,
        )

    async def get_top_videos(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        days: Optional[int],
        start_date: Optional[date],
        end_date: Optional[date],
        limit: int,
    ) -> TopVideosResponse:
        account = await self._get_analytics_account(user_id, account_id)
        rng = resolve_range(days, start_date, end_date)
        key = report_cache_key("top_videos", rng.start, rng.end, limit)
        batch = await self._fetch_reports(
            user_id,
            account,
            {
                key: {
                    "metrics": _TOP_VIDEO_METRICS,
                    "dimensions": "video",
                    "sort": "-views",
                    "max_results": limit,
                }
            },
            range_for_query=rng,
        )
        batch.raise_if_failed()

        rows = batch.rows(key)
        known = await analytics_crud.get_videos_by_ids(
            self._session, account.id, [str(row.get("video")) for row in rows]
        )
        videos = []
        for row in rows:
            video_id = str(row.get("video"))
            stored = known.get(video_id)
            videos.append(
                TopVideoRow(
                    video_id=video_id,
                    title=stored.title if stored else video_id,
                    thumbnail_url=stored.thumbnail_url if stored else None,
                    published_at=stored.published_at if stored else None,
                    is_short=stored.is_short if stored else False,
                    views=int(number(row.get("views"))),
                    estimated_minutes_watched=int(number(row.get("estimatedMinutesWatched"))),
                    average_view_duration_seconds=round(number(row.get("averageViewDuration")), 1),
                    average_view_percentage=round(number(row.get("averageViewPercentage")), 1),
                    likes=int(number(row.get("likes"))),
                    comments=int(number(row.get("comments"))),
                    shares=int(number(row.get("shares"))),
                    subscribers_gained=int(number(row.get("subscribersGained"))),
                    performance_index=stored.performance_index if stored else None,
                )
            )
        return TopVideosResponse(
            account_id=account.id,
            start_date=rng.start,
            end_date=rng.end,
            videos=videos,
            fetched_at=batch.fetched_at,
        )

    async def list_videos(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        search: Optional[str],
        content_type: ContentType,
        privacy_status: Optional[str],
        date_from: Optional[datetime],
        date_to: Optional[datetime],
        sort: VideoSort,
        order: SortOrder,
        limit: int,
        offset: int,
    ) -> VideoListResponse:
        account = await self._get_account(user_id, account_id)
        videos, total = await analytics_crud.list_videos(
            self._session,
            account.id,
            search=search,
            content_type=content_type,
            privacy_status=privacy_status,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
        return VideoListResponse(
            videos=[VideoResponse.model_validate(video) for video in videos],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def _get_stored_video(self, account_id: uuid.UUID, video_id: str) -> YtVideo:
        video = await analytics_crud.get_video(self._session, account_id, video_id)
        if video is None:
            raise NotFoundError(
                "This video was not found in the loaded channel data. Sync the channel and try again."
            )
        return video

    async def get_video_detail(
        self, user_id: uuid.UUID, account_id: uuid.UUID, video_id: str
    ) -> VideoDetailResponse:
        account = await self._get_account(user_id, account_id)
        video = await self._get_stored_video(account.id, video_id)
        group = [
            stored
            for stored in await analytics_crud.get_all_videos(self._session, account.id)
            if stored.is_short == video.is_short
        ]
        views = [stored.view_count for stored in group]
        rates = [stored.engagement_rate for stored in group if stored.view_count > 0]
        median_views = int(statistics.median(views)) if views else None
        average_rate = round(statistics.fmean(rates), 2) if rates else None
        rank = 1 + sum(1 for value in views if value > video.view_count)

        comparison = VideoComparison(
            rank_by_views=rank,
            total_videos=len(group),
            channel_median_views=median_views,
            channel_average_engagement_rate=average_rate,
            views_vs_median=round(video.view_count / median_views, 2) if median_views else None,
            engagement_vs_average=round(video.engagement_rate / average_rate, 2)
            if average_rate
            else None,
            performance_label=performance_label(video.performance_index),
        )
        return VideoDetailResponse(
            account_id=account.id,
            video=VideoResponse.model_validate(video),
            description=video.description,
            comparison=comparison,
        )

    async def get_video_analytics(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        video_id: str,
        *,
        days: Optional[int],
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> VideoAnalyticsResponse:
        account = await self._get_analytics_account(user_id, account_id)
        video = await self._get_stored_video(account.id, video_id)

        if days is None and start_date is None and end_date is None:
            latest = resolve_range(1, None, None).end
            earliest = latest - timedelta(days=settings.yt_analytics_max_days - 1)
            start = max(ensure_utc(video.published_at).date(), earliest)
            rng = DateRange(start=min(start, latest), end=latest)
        else:
            rng = resolve_range(days, start_date, end_date)

        names = ("daily", "retention", "traffic", "search", "demographics")
        keys = {name: report_cache_key(f"video_{name}", video.video_id, rng.start, rng.end) for name in names}
        video_filter = f"video=={video.video_id}"
        batch = await self._fetch_reports(
            user_id,
            account,
            {
                keys["daily"]: {
                    "metrics": _DAILY_METRICS,
                    "dimensions": "day",
                    "filters": video_filter,
                    "sort": "day",
                },
                keys["retention"]: {
                    "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                    "dimensions": "elapsedVideoTimeRatio",
                    "filters": video_filter,
                },
                keys["traffic"]: {
                    "metrics": _VIEW_METRICS,
                    "dimensions": "insightTrafficSourceType",
                    "filters": video_filter,
                    "sort": "-views",
                },
                keys["search"]: {
                    "metrics": _VIEW_METRICS,
                    "dimensions": "insightTrafficSourceDetail",
                    "filters": f"{video_filter};insightTrafficSourceType==YT_SEARCH",
                    "sort": "-views",
                    "max_results": settings.yt_analytics_search_terms_limit,
                },
                keys["demographics"]: {
                    "metrics": "viewerPercentage",
                    "dimensions": "ageGroup,gender",
                    "filters": video_filter,
                    "sort": "gender,ageGroup",
                },
            },
            range_for_query=rng,
        )

        daily_rows = batch.rows(keys["daily"])
        return VideoAnalyticsResponse(
            account_id=account.id,
            video_id=video.video_id,
            start_date=rng.start,
            end_date=rng.end,
            totals=MetricTotals(**summarize_daily(daily_rows)),
            daily=batch.section(keys["daily"], daily_points),
            retention=batch.section(keys["retention"], _retention_rows),
            traffic_sources=batch.section(
                keys["traffic"], lambda rows: breakdown_rows(rows, "insightTrafficSourceType")
            ),
            search_terms=batch.section(
                keys["search"], lambda rows: breakdown_rows(rows, "insightTrafficSourceDetail")
            ),
            demographics=batch.section(keys["demographics"], _demographic_rows),
            fetched_at=batch.fetched_at,
        )

    async def get_video_comments(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        video_id: str,
        *,
        limit: Optional[int] = None,
    ) -> VideoCommentsResponse:
        account = await self._get_account(user_id, account_id)
        video = await self._get_stored_video(account.id, video_id)
        if COMMENTS_SCOPE not in (account.scopes or "").split():
            raise CommentsPermissionMissingError()
        key = report_cache_key("comments", video.video_id)

        cached = await analytics_crud.get_cache(self._session, account.id, key)
        if cached is not None:
            payload = cached.payload
            fetched_at = ensure_utc(cached.fetched_at)
        else:
            credentials = await self._credentials(user_id, account)
            try:
                comments = await asyncio.to_thread(
                    yt_api.fetch_comments_sync,
                    credentials,
                    video.video_id,
                    limit=settings.yt_analytics_comments_limit,
                )
                payload = {"comments": comments, "disabled": False}
            except Exception as exc:
                if comments_disabled(exc):
                    payload = {"comments": [], "disabled": True}
                elif is_permission_error(exc):
                    raise CommentsPermissionMissingError() from exc
                else:
                    logger.warning("Reading comments of %s failed: %s", video.video_id, exc)
                    raise classify_youtube_error(exc) from exc
            record = await analytics_crud.set_cache(
                self._session,
                account_id=account.id,
                cache_key=key,
                payload=payload,
                ttl_seconds=settings.yt_analytics_comments_ttl_seconds,
            )
            await self._session.commit()
            fetched_at = ensure_utc(record.fetched_at)

        comments = payload.get("comments", [])
        if limit is not None:
            comments = comments[:limit]
        return VideoCommentsResponse(
            account_id=account.id,
            video_id=video.video_id,
            comments=[CommentItem(**comment) for comment in comments],
            comments_disabled=bool(payload.get("disabled", False)),
            fetched_at=fetched_at,
        )

    async def load_points(
        self, account_id: uuid.UUID, content_type: ContentType = ContentType.ALL
    ) -> list[VideoPoint]:
        videos = await analytics_crud.get_all_videos(self._session, account_id)
        return filter_points([_to_point(video) for video in videos], content_type)

    async def get_publishing_insights(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        timezone_name: Optional[str],
        content_type: ContentType,
    ) -> PublishingInsightsResponse:
        account = await self._get_account(user_id, account_id)
        points = await self.load_points(account.id, content_type)
        zone = timezone_name or settings.yt_analytics_default_timezone
        insights = build_publishing_insights(points, zone)
        return PublishingInsightsResponse(
            account_id=account.id,
            content_type=content_type.value,
            cadence=build_cadence_insights(points),
            **insights,
        )

    async def get_content_insights(
        self, user_id: uuid.UUID, account_id: uuid.UUID, *, content_type: ContentType
    ) -> ContentInsightsResponse:
        account = await self._get_account(user_id, account_id)
        points = await self.load_points(account.id, content_type)
        scored = [point for point in points if point.performance_index is not None]
        ranked = rank_videos(points, 10, with_tags=True)
        if len(scored) < settings.yt_analytics_min_videos_for_insights:
            note = (
                "There are too few videos with enough history for strong conclusions. "
                "Use these patterns as hints and revisit after more uploads."
            )
        else:
            note = (
                "Patterns compare each video with other videos of the same format published "
                "around the same time, so older videos are not unfairly ahead."
            )
        return ContentInsightsResponse(
            account_id=account.id,
            content_type=content_type.value,
            sample_size=len(scored),
            confidence=confidence_for(len(scored)),
            best_videos=ranked["best"],
            worst_videos=ranked["worst"],
            tags=build_tag_insights(points),
            titles=build_title_insights(points),
            durations=build_duration_insights(points),
            note=note,
        )

    async def get_portfolio_overview(self, user_id: uuid.UUID) -> PortfolioOverviewResponse:
        accounts = [
            account
            for account in await yt_crud.get_accounts_by_user(self._session, user_id)
            if account.is_active
        ]
        account_ids = [account.id for account in accounts]
        syncs = await analytics_crud.get_syncs_by_accounts(self._session, account_ids)
        aggregates = await analytics_crud.get_video_aggregates(
            self._session, account_ids, since=utc_now() - timedelta(days=30)
        )

        channels: list[PortfolioChannel] = []
        for account in accounts:
            latest, growth = await self._growth(account.id)
            aggregate = aggregates.get(account.id, {})
            top = await analytics_crud.get_top_video(self._session, account.id)
            channels.append(
                PortfolioChannel(
                    account_id=account.id,
                    nickname=account.nickname,
                    channel_title=account.channel_title,
                    channel_thumbnail_url=account.channel_thumbnail_url,
                    subscribers=latest.subscribers if latest else None,
                    total_views=latest.total_views if latest else 0,
                    channel_video_count=latest.video_count if latest else 0,
                    synced_videos=aggregate.get("video_count", 0),
                    recent_uploads_30d=aggregate.get("recent_uploads", 0),
                    subscribers_28d=growth.subscribers_28d,
                    views_28d=growth.views_28d,
                    average_performance_index=round(aggregate["average_performance_index"], 2)
                    if aggregate.get("average_performance_index") is not None
                    else None,
                    last_published_at=aggregate.get("last_published_at"),
                    top_video={
                        "video_id": top.video_id,
                        "title": top.title,
                        "views": top.view_count,
                        "thumbnail_url": top.thumbnail_url,
                    }
                    if top
                    else None,
                    analytics_enabled=_has_analytics_scope(account) and not account.needs_reconnect,
                    needs_reconnect=account.needs_reconnect,
                    sync=self._sync_response(account.id, syncs.get(account.id)),
                )
            )

        return PortfolioOverviewResponse(
            total_channels=len(channels),
            total_subscribers=sum(channel.subscribers or 0 for channel in channels),
            total_views=sum(channel.total_views for channel in channels),
            total_videos=sum(channel.channel_video_count for channel in channels),
            channels=channels,
        )


async def run_sync_job(user_id: uuid.UUID, account_id: uuid.UUID) -> None:
    async with SessionFactory() as session:
        await YtAnalyticsService(session).execute_sync(user_id, account_id)
# app/service/yt_ai_insights.py
import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any, Optional

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    AiGenerationError,
    AiLimitReachedError,
    AiNotConfiguredError,
    AnalyticsNotSyncedError,
    AppException,
    CommentsPermissionMissingError,
    NotFoundError,
)
from app.crud import yt_ai_insights as ai_crud
from app.crud import yt_management as yt_crud
from app.integrations.gemini_api import GeminiError, generate_json_sync
from app.models.yt_ai_insights import YtAiReport
from app.schemas.yt_ai_insights import (
    AiReportContent,
    AiReportListResponse,
    AiReportResponse,
    AiReportSummary,
    CreateAiReportRequest,
    CreatePortfolioReportRequest,
)
from app.service.yt_analytics import YtAnalyticsService
from app.utils.datetime_utils import ensure_utc, utc_now
from app.utils.yt_ai_insights import (
    SYSTEM_PROMPT,
    AiLanguage,
    AiReportType,
    build_retry_prompt,
    build_user_prompt,
    input_hash,
    model_for,
    parse_model_json,
)
from app.utils.yt_analytics import (
    ContentType,
    bucket_points,
    build_cadence_insights,
    clip,
    downsample,
    rank_videos,
    resolve_range,
)

logger = logging.getLogger(__name__)

_COMMENT_TEXT_LIMIT = 300
_PORTFOLIO_MAX_CHANNELS = 12


def _rows(section, limit: int) -> dict[str, Any]:
    dumped = section.model_dump(mode="json")
    return {
        "available": dumped["available"],
        "message": dumped["message"],
        "rows": dumped["rows"][:limit],
    }


class YtAiInsightsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._analytics = YtAnalyticsService(session)

    async def create_report(
        self, user_id: uuid.UUID, account_id: uuid.UUID, payload: CreateAiReportRequest
    ) -> AiReportResponse:
        self._check_ready()
        account = await yt_crud.get_account_by_id(self._session, account_id, user_id)
        if account is None:
            raise NotFoundError("YouTube account not found.")
        channel = await self._analytics.get_channel(user_id, account_id)
        if not channel.has_data:
            raise AnalyticsNotSyncedError()

        missing: list[str] = []
        data = await self._collect_account_data(
            user_id, account_id, channel, payload, missing
        )
        return await self._generate(
            user_id,
            account_id=account.id,
            video_id=payload.video_id,
            report_type=payload.report_type,
            days=payload.days,
            language=payload.language,
            data=data,
            data_quality=self._data_quality(payload.days, channel, missing),
            force=payload.force,
        )

    async def create_portfolio_report(
        self, user_id: uuid.UUID, payload: CreatePortfolioReportRequest
    ) -> AiReportResponse:
        self._check_ready()
        overview = await self._analytics.get_portfolio_overview(user_id)
        synced = [channel for channel in overview.channels if channel.sync.last_synced_at]
        if not synced:
            raise AnalyticsNotSyncedError(
                "None of your channels has been loaded yet. Open a channel dashboard and sync it first."
            )

        missing: list[str] = []
        channels = []
        for channel in overview.channels[:_PORTFOLIO_MAX_CHANNELS]:
            entry = channel.model_dump(mode="json", exclude={"channel_thumbnail_url", "sync"})
            entry["synced"] = channel.sync.last_synced_at is not None
            if not entry["synced"]:
                missing.append(f"{channel.channel_title}: not synced yet")
            else:
                points = await self._analytics.load_points(channel.account_id)
                ranked = rank_videos(points, 3)
                content = await self._analytics.get_content_insights(
                    user_id, channel.account_id, content_type=ContentType.ALL
                )
                entry["best_videos"] = ranked["best"]
                entry["worst_videos"] = ranked["worst"]
                entry["top_tags"] = content.tags.get("top_tags", [])[:5]
                entry["duration_groups"] = content.durations
                entry["cadence"] = build_cadence_insights(points)
            if channel.needs_reconnect:
                missing.append(f"{channel.channel_title}: needs to be reconnected")
            elif not channel.analytics_enabled:
                missing.append(f"{channel.channel_title}: analytics permission not granted")
            channels.append(entry)

        data = {
            "portfolio": {
                "total_channels": overview.total_channels,
                "total_subscribers": overview.total_subscribers,
                "total_views": overview.total_views,
                "total_videos": overview.total_videos,
            },
            "channels": channels,
        }
        data_quality = {
            "period_days": payload.days,
            "channels_total": overview.total_channels,
            "channels_with_data": len(synced),
            "analytics_available": all(channel.analytics_enabled for channel in overview.channels),
            "sections_missing": missing,
            "generated_at": utc_now().isoformat(),
        }
        return await self._generate(
            user_id,
            account_id=None,
            video_id=None,
            report_type=AiReportType.PORTFOLIO,
            days=payload.days,
            language=payload.language,
            data=data,
            data_quality=data_quality,
            force=payload.force,
        )

    async def list_reports(
        self,
        user_id: uuid.UUID,
        *,
        account_id: Optional[uuid.UUID],
        video_id: Optional[str],
        report_type: Optional[AiReportType],
        limit: int,
        offset: int,
    ) -> AiReportListResponse:
        reports, total = await ai_crud.list_reports(
            self._session,
            user_id,
            account_id=account_id,
            video_id=video_id,
            report_type=report_type.value if report_type else None,
            limit=limit,
            offset=offset,
        )
        return AiReportListResponse(
            reports=[
                AiReportSummary(
                    id=report.id,
                    account_id=report.account_id,
                    video_id=report.video_id,
                    report_type=report.report_type,
                    period_days=report.period_days,
                    language=report.language,
                    model=report.model,
                    created_at=report.created_at,
                    headline=(report.result or {}).get("headline"),
                    score=(report.result or {}).get("score"),
                )
                for report in reports
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_report(self, user_id: uuid.UUID, report_id: uuid.UUID) -> AiReportResponse:
        report = await ai_crud.get_report(self._session, user_id, report_id)
        if report is None:
            raise NotFoundError("AI report not found.")
        return self._to_response(report, cached=False)

    async def delete_report(self, user_id: uuid.UUID, report_id: uuid.UUID) -> None:
        report = await ai_crud.get_report(self._session, user_id, report_id)
        if report is None:
            raise NotFoundError("AI report not found.")
        await ai_crud.delete_report(self._session, report)
        await self._session.commit()

    def _check_ready(self) -> None:
        if not settings.gemini_configured:
            raise AiNotConfiguredError()

    def _to_response(self, report: YtAiReport, *, cached: bool) -> AiReportResponse:
        return AiReportResponse(
            id=report.id,
            account_id=report.account_id,
            video_id=report.video_id,
            report_type=report.report_type,
            period_days=report.period_days,
            language=report.language,
            model=report.model,
            created_at=report.created_at,
            cached=cached,
            content=AiReportContent.model_validate(report.result),
            data_quality=report.data_quality or {},
        )

    def _data_quality(self, days: int, channel, missing: list[str]) -> dict[str, Any]:
        rng = resolve_range(days, None, None)
        return {
            "period_days": days,
            "start_date": rng.start.isoformat(),
            "end_date": rng.end.isoformat(),
            "analytics_available": channel.analytics_enabled,
            "videos_in_database": channel.videos.total,
            "last_synced_at": channel.sync.last_synced_at.isoformat()
            if channel.sync.last_synced_at
            else None,
            "sections_missing": missing,
            "generated_at": utc_now().isoformat(),
        }

    async def _safe(self, name: str, awaitable, missing: list[str]):
        try:
            return await awaitable
        except AppException as exc:
            missing.append(f"{name}: {exc.message}")
            return None

    def _channel_block(self, channel, *, light: bool = False) -> dict[str, Any]:
        block: dict[str, Any] = {
            "name": channel.channel_title,
            "subscribers": channel.subscribers,
            "subscribers_hidden": channel.hidden_subscriber_count,
            "total_views": channel.total_views,
            "channel_video_count": channel.channel_video_count,
            "videos_loaded": channel.videos.model_dump(),
            "median_views_per_video": channel.video_totals.median_views,
            "average_engagement_rate": channel.video_totals.average_engagement_rate,
        }
        if not light:
            block["growth_from_snapshots"] = channel.growth.model_dump()
            block["lifetime_video_likes"] = channel.video_totals.likes
            block["lifetime_video_comments"] = channel.video_totals.comments
        return block

    async def _performance_block(
        self, user_id: uuid.UUID, account_id: uuid.UUID, days: int, missing: list[str]
    ) -> Optional[dict[str, Any]]:
        result = await self._safe(
            "performance",
            self._analytics.get_performance(
                user_id, account_id, days=days, start_date=None, end_date=None
            ),
            missing,
        )
        if result is None:
            return None
        daily = [point.model_dump() for point in result.daily]
        return {
            "period": {"from": result.start_date.isoformat(), "to": result.end_date.isoformat()},
            "current": result.current.model_dump(),
            "previous": result.previous.model_dump(),
            "change_percent": result.change_percent,
            "weekly": bucket_points(daily, 7),
        }

    async def _top_videos_block(
        self, user_id: uuid.UUID, account_id: uuid.UUID, days: int, missing: list[str], limit: int = 15
    ) -> Optional[list[dict[str, Any]]]:
        result = await self._safe(
            "top videos",
            self._analytics.get_top_videos(
                user_id, account_id, days=days, start_date=None, end_date=None, limit=limit
            ),
            missing,
        )
        if result is None:
            return None
        return [
            video.model_dump(mode="json", exclude={"thumbnail_url", "estimated_minutes_watched"})
            for video in result.videos
        ]

    async def _audience_block(
        self, user_id: uuid.UUID, account_id: uuid.UUID, days: int, missing: list[str]
    ) -> Optional[dict[str, Any]]:
        result = await self._safe(
            "audience",
            self._analytics.get_audience(
                user_id, account_id, days=days, start_date=None, end_date=None
            ),
            missing,
        )
        if result is None:
            return None
        return {
            "demographics": _rows(result.demographics, 12),
            "countries": _rows(result.countries, 8),
            "devices": _rows(result.devices, 6),
            "subscribed_status": _rows(result.subscribed_status, 4),
        }

    async def _traffic_block(
        self, user_id: uuid.UUID, account_id: uuid.UUID, days: int, missing: list[str]
    ) -> Optional[dict[str, Any]]:
        result = await self._safe(
            "traffic",
            self._analytics.get_traffic(
                user_id, account_id, days=days, start_date=None, end_date=None
            ),
            missing,
        )
        if result is None:
            return None
        return {
            "sources": _rows(result.sources, 10),
            "search_terms": _rows(result.search_terms, 15),
            "content_types": _rows(result.content_types, 5),
        }

    async def _content_block(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        missing: list[str],
        *,
        full: bool,
    ) -> Optional[dict[str, Any]]:
        result = await self._safe(
            "content patterns",
            self._analytics.get_content_insights(
                user_id, account_id, content_type=ContentType.ALL
            ),
            missing,
        )
        if result is None:
            return None
        depth = 8 if full else 5
        tags = result.tags
        block: dict[str, Any] = {
            "sample_size": result.sample_size,
            "confidence": result.confidence,
            "best_videos": result.best_videos[:depth],
            "worst_videos": result.worst_videos[:depth],
            "duration_groups": result.durations,
        }
        if full:
            block["tags"] = {
                "top_tags": tags.get("top_tags", [])[:12],
                "weak_tags": tags.get("weak_tags", [])[:8],
                "most_used_tags": tags.get("most_used_tags", [])[:10],
                "tag_count_groups": tags.get("tag_count_groups", []),
                "average_tags_per_video": tags.get("average_tags_per_video"),
            }
            block["titles"] = result.titles
        return block

    async def _publishing_block(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        timezone_name: Optional[str],
        missing: list[str],
        *,
        full: bool,
    ) -> Optional[dict[str, Any]]:
        result = await self._safe(
            "publishing times",
            self._analytics.get_publishing_insights(
                user_id,
                account_id,
                timezone_name=timezone_name,
                content_type=ContentType.ALL,
            ),
            missing,
        )
        if result is None:
            return None
        block: dict[str, Any] = {
            "timezone": result.timezone,
            "sample_size": result.sample_size,
            "confidence": result.confidence,
            "best_weekday": result.best_weekday,
            "best_hour": result.best_hour,
            "best_day_part": result.best_day_part,
            "best_slots": result.best_slots,
            "by_weekday": result.by_weekday,
            "by_day_part": result.by_day_part,
            "cadence": result.cadence,
        }
        if full:
            block["by_hour"] = [row for row in result.by_hour if row["videos"] > 0]
        return block

    async def _comments_block(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        missing: list[str],
        *,
        videos: int,
        per_video: int,
    ) -> list[dict[str, Any]]:
        points = await self._analytics.load_points(account_id)
        candidates = sorted(
            (point for point in points if (point.comments or 0) > 0),
            key=lambda point: (point.views, point.performance_index or 0),
            reverse=True,
        )[:videos]

        blocks = []
        for point in candidates:
            try:
                result = await self._analytics.get_video_comments(
                    user_id, account_id, point.video_id, limit=per_video
                )
            except CommentsPermissionMissingError as exc:
                missing.append(f"comments: {exc.message}")
                break
            except AppException as exc:
                missing.append(f"comments of {point.title}: {exc.message}")
                continue
            if not result.comments:
                continue
            blocks.append(
                {
                    "video_title": point.title,
                    "video_id": point.video_id,
                    "performance_index": point.performance_index,
                    "total_comments_on_video": point.comments,
                    "comments": [
                        {"text": clip(comment.text, _COMMENT_TEXT_LIMIT), "likes": comment.like_count}
                        for comment in result.comments
                    ],
                }
            )
        return blocks

    async def _video_block(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        video_id: str,
        missing: list[str],
    ) -> dict[str, Any]:
        detail = await self._analytics.get_video_detail(user_id, account_id, video_id)
        video = detail.video
        block: dict[str, Any] = {
            "video": {
                "title": video.title,
                "description": clip(detail.description, settings.yt_ai_description_max_chars),
                "tags": video.tags,
                "duration_seconds": video.duration_seconds,
                "is_short": video.is_short,
                "published_at": video.published_at.isoformat(),
                "privacy_status": video.privacy_status,
                "views": video.view_count,
                "likes": video.like_count,
                "comments": video.comment_count,
                "engagement_rate": video.engagement_rate,
                "performance_index": video.performance_index,
            },
            "comparison": detail.comparison.model_dump(),
        }

        analytics = await self._safe(
            "video analytics",
            self._analytics.get_video_analytics(
                user_id, account_id, video_id, days=None, start_date=None, end_date=None
            ),
            missing,
        )
        if analytics is not None:
            daily = analytics.daily.model_dump(mode="json")
            points = daily["rows"]
            block["analytics"] = {
                "period": {
                    "from": analytics.start_date.isoformat(),
                    "to": analytics.end_date.isoformat(),
                },
                "totals": analytics.totals.model_dump(),
                "views_over_time": points if len(points) <= 45 else bucket_points(points, 7),
                "retention_curve": downsample(
                    analytics.retention.model_dump(mode="json")["rows"], 20
                ),
                "traffic_sources": _rows(analytics.traffic_sources, 8),
                "search_terms": _rows(analytics.search_terms, 12),
                "demographics": _rows(analytics.demographics, 12),
            }

        points_same_format = [
            point
            for point in await self._analytics.load_points(account_id)
            if point.is_short == video.is_short and point.video_id != video_id
        ]
        block["best_videos_same_format"] = rank_videos(points_same_format, 5)["best"]

        comments = await self._safe(
            "comments",
            self._analytics.get_video_comments(
                user_id, account_id, video_id, limit=settings.yt_ai_comments_per_video
            ),
            missing,
        )
        if comments is not None and comments.comments:
            block["comments"] = [
                {"text": clip(comment.text, _COMMENT_TEXT_LIMIT), "likes": comment.like_count}
                for comment in comments.comments
            ]
        return block

    async def _collect_account_data(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        channel,
        payload: CreateAiReportRequest,
        missing: list[str],
    ) -> dict[str, Any]:
        report_type = payload.report_type
        days = payload.days
        data: dict[str, Any] = {"channel": self._channel_block(channel)}

        if report_type is AiReportType.CHANNEL_STRATEGY:
            data["performance"] = await self._performance_block(user_id, account_id, days, missing)
            data["top_videos_in_period"] = await self._top_videos_block(user_id, account_id, days, missing)
            data["audience"] = await self._audience_block(user_id, account_id, days, missing)
            data["traffic"] = await self._traffic_block(user_id, account_id, days, missing)
            data["content"] = await self._content_block(user_id, account_id, missing, full=False)
            data["publishing"] = await self._publishing_block(
                user_id, account_id, payload.timezone, missing, full=False
            )
        elif report_type is AiReportType.CONTENT_ANALYSIS:
            data["content"] = await self._content_block(user_id, account_id, missing, full=True)
            data["top_videos_in_period"] = await self._top_videos_block(user_id, account_id, days, missing)
            data["traffic"] = await self._traffic_block(user_id, account_id, days, missing)
            data["publishing"] = await self._publishing_block(
                user_id, account_id, payload.timezone, missing, full=False
            )
        elif report_type is AiReportType.PUBLISHING_SCHEDULE:
            data["publishing"] = await self._publishing_block(
                user_id, account_id, payload.timezone, missing, full=True
            )
            audience = await self._audience_block(user_id, account_id, days, missing)
            data["audience_countries"] = audience["countries"] if audience else None
            data["performance"] = await self._performance_block(user_id, account_id, days, missing)
        elif report_type is AiReportType.AUDIENCE_KEYWORDS:
            data["audience"] = await self._audience_block(user_id, account_id, days, missing)
            data["traffic"] = await self._traffic_block(user_id, account_id, days, missing)
            data["content"] = await self._content_block(user_id, account_id, missing, full=True)
            data["top_videos_in_period"] = await self._top_videos_block(user_id, account_id, days, missing)
        elif report_type is AiReportType.COMMENTS_INSIGHTS:
            data["channel"] = self._channel_block(channel, light=True)
            data["comments"] = await self._comments_block(
                user_id,
                account_id,
                missing,
                videos=settings.yt_ai_comment_videos,
                per_video=settings.yt_ai_comments_per_video,
            )
            data["content"] = await self._content_block(user_id, account_id, missing, full=False)
        elif report_type is AiReportType.CONTENT_PLAN:
            data["content"] = await self._content_block(user_id, account_id, missing, full=True)
            data["publishing"] = await self._publishing_block(
                user_id, account_id, payload.timezone, missing, full=False
            )
            audience = await self._audience_block(user_id, account_id, days, missing)
            data["audience"] = audience
            traffic = await self._traffic_block(user_id, account_id, days, missing)
            data["search_terms"] = traffic["search_terms"] if traffic else None
            data["comments"] = await self._comments_block(
                user_id, account_id, missing, videos=3, per_video=25
            )
        elif report_type is AiReportType.VIDEO_REVIEW:
            data["channel"] = self._channel_block(channel, light=True)
            data.update(await self._video_block(user_id, account_id, payload.video_id, missing))

        return {key: value for key, value in data.items() if value is not None}

    async def _generate(
        self,
        user_id: uuid.UUID,
        *,
        account_id: Optional[uuid.UUID],
        video_id: Optional[str],
        report_type: AiReportType,
        days: int,
        language: AiLanguage,
        data: dict[str, Any],
        data_quality: dict[str, Any],
        force: bool,
    ) -> AiReportResponse:
        data = {**data, "data_quality": data_quality}
        stable = {key: value for key, value in data.items() if key != "data_quality"}
        stable["data_quality"] = {
            key: value
            for key, value in data_quality.items()
            if key not in {"generated_at", "last_synced_at"}
        }
        digest = input_hash(report_type, language, days, stable)
        now = utc_now()

        cached = await ai_crud.find_cached(
            self._session,
            user_id=user_id,
            account_id=account_id,
            video_id=video_id,
            report_type=report_type.value,
            input_hash=digest,
            since=now - timedelta(hours=settings.yt_ai_report_cache_hours),
        )
        if cached is not None and not force:
            return self._to_response(cached, cached=True)

        last = await ai_crud.latest_created_at(
            self._session,
            user_id=user_id,
            account_id=account_id,
            video_id=video_id,
            report_type=report_type.value,
        )
        if force and last is not None:
            wait = settings.yt_ai_force_cooldown_seconds - (now - ensure_utc(last)).total_seconds()
            if wait > 0:
                if cached is not None:
                    return self._to_response(cached, cached=True)
                raise AiLimitReachedError(
                    f"This report was just created. Please wait {int(wait) + 1} seconds "
                    "before creating it again."
                )

        used_today = await ai_crud.count_since(self._session, user_id, now - timedelta(hours=24))
        if used_today >= settings.yt_ai_daily_report_limit:
            raise AiLimitReachedError(
                f"You have created {settings.yt_ai_daily_report_limit} AI reports in the last 24 hours. "
                "Please try again later."
            )

        model = model_for(report_type)
        base_prompt = build_user_prompt(
            report_type, language, today=now.date().isoformat(), data=data
        )
        content = await self._ask_gemini(model, base_prompt)

        report = await ai_crud.create_report(
            self._session,
            user_id=user_id,
            account_id=account_id,
            video_id=video_id,
            report_type=report_type.value,
            period_days=days,
            language=language.value,
            model=model,
            input_hash=digest,
            result=content.model_dump(mode="json"),
            data_quality=data_quality,
        )
        await self._session.commit()
        logger.info(
            "AI report %s (%s) created for user %s", report.id, report_type.value, user_id
        )
        return self._to_response(report, cached=False)

    async def _ask_gemini(self, model: str, base_prompt: str) -> AiReportContent:
        prompt = base_prompt
        problem = ""
        for attempt in range(1, settings.gemini_max_attempts + 1):
            try:
                text = await asyncio.to_thread(
                    generate_json_sync,
                    model=model,
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=prompt,
                )
            except GeminiError as exc:
                if exc.kind in {"truncated", "empty"} and attempt < settings.gemini_max_attempts:
                    problem = exc.message
                    prompt = build_retry_prompt(base_prompt, problem)
                    continue
                raise self._map_gemini_error(exc) from exc

            try:
                return AiReportContent.model_validate(parse_model_json(text))
            except (ValueError, PydanticValidationError) as exc:
                logger.warning("Gemini answer was not usable on attempt %s: %s", attempt, exc)
                problem = "It was not valid JSON for the required output shape."
                prompt = build_retry_prompt(base_prompt, problem)

        raise AiGenerationError()

    def _map_gemini_error(self, exc: GeminiError) -> AppException:
        if exc.kind == "not_configured":
            return AiNotConfiguredError()
        if exc.kind == "auth":
            return AiNotConfiguredError(
                "The AI service rejected the server configuration. Please contact support."
            )
        if exc.kind == "rate_limited":
            return AiGenerationError(
                "The AI service is busy right now. Please try again in a minute.", code="ai_busy"
            )
        if exc.kind == "blocked":
            return AiGenerationError(
                "The AI could not produce a report for this content.", code="ai_blocked"
            )
        return AiGenerationError()
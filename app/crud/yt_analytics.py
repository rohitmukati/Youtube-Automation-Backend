# app/crud/yt_analytics.py
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.yt_analytics import (
    YtAnalyticsCache,
    YtAnalyticsSync,
    YtChannelSnapshot,
    YtVideo,
)
from app.utils.datetime_utils import utc_now
from app.utils.yt_analytics import (
    ContentType,
    SortOrder,
    SyncStatus,
    VideoSort,
    engagement_rate,
)

_ID_CHUNK = 500


async def get_sync(session: AsyncSession, account_id: uuid.UUID) -> Optional[YtAnalyticsSync]:
    result = await session.execute(
        select(YtAnalyticsSync)
        .where(YtAnalyticsSync.account_id == account_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_syncs_by_accounts(
    session: AsyncSession, account_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, YtAnalyticsSync]:
    if not account_ids:
        return {}
    result = await session.execute(
        select(YtAnalyticsSync).where(YtAnalyticsSync.account_id.in_(account_ids))
    )
    return {record.account_id: record for record in result.scalars().all()}


async def claim_sync(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    stale_before: datetime,
) -> bool:
    record = await get_sync(session, account_id)
    now = utc_now()
    if record is None:
        try:
            async with session.begin_nested():
                session.add(
                    YtAnalyticsSync(
                        account_id=account_id,
                        user_id=user_id,
                        status=SyncStatus.RUNNING.value,
                        started_at=now,
                        videos_synced=0,
                    )
                )
                await session.flush()
            return True
        except IntegrityError:
            return False

    result = await session.execute(
        update(YtAnalyticsSync)
        .where(
            YtAnalyticsSync.id == record.id,
            or_(
                YtAnalyticsSync.status != SyncStatus.RUNNING.value,
                YtAnalyticsSync.started_at.is_(None),
                YtAnalyticsSync.started_at < stale_before,
            ),
        )
        .values(
            status=SyncStatus.RUNNING.value,
            started_at=now,
            finished_at=None,
            last_error=None,
            updated_at=now,
        )
    )
    return (result.rowcount or 0) == 1


async def finish_sync(
    session: AsyncSession,
    record: YtAnalyticsSync,
    *,
    succeeded: bool,
    videos_synced: int = 0,
    error: Optional[str] = None,
) -> YtAnalyticsSync:
    now = utc_now()
    record.status = SyncStatus.COMPLETED.value if succeeded else SyncStatus.FAILED.value
    record.finished_at = now
    record.last_error = None if succeeded else error
    if succeeded:
        record.last_synced_at = now
        record.videos_synced = videos_synced
    await session.flush()
    return record


async def upsert_snapshot(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    snapshot_date: date,
    subscribers: Optional[int],
    hidden_subscriber_count: bool,
    total_views: int,
    video_count: int,
) -> YtChannelSnapshot:
    result = await session.execute(
        select(YtChannelSnapshot).where(
            YtChannelSnapshot.account_id == account_id,
            YtChannelSnapshot.snapshot_date == snapshot_date,
        )
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        snapshot = YtChannelSnapshot(account_id=account_id, snapshot_date=snapshot_date)
        session.add(snapshot)
    snapshot.subscribers = subscribers
    snapshot.hidden_subscriber_count = hidden_subscriber_count
    snapshot.total_views = total_views
    snapshot.video_count = video_count
    await session.flush()

    cutoff = snapshot_date - timedelta(days=settings.yt_analytics_snapshot_retention_days)
    await session.execute(
        delete(YtChannelSnapshot).where(
            YtChannelSnapshot.account_id == account_id,
            YtChannelSnapshot.snapshot_date < cutoff,
        )
    )
    return snapshot


async def get_latest_snapshot(
    session: AsyncSession, account_id: uuid.UUID
) -> Optional[YtChannelSnapshot]:
    result = await session.execute(
        select(YtChannelSnapshot)
        .where(YtChannelSnapshot.account_id == account_id)
        .order_by(YtChannelSnapshot.snapshot_date.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_snapshot_on_or_before(
    session: AsyncSession, account_id: uuid.UUID, target: date
) -> Optional[YtChannelSnapshot]:
    result = await session.execute(
        select(YtChannelSnapshot)
        .where(
            YtChannelSnapshot.account_id == account_id,
            YtChannelSnapshot.snapshot_date <= target,
        )
        .order_by(YtChannelSnapshot.snapshot_date.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_snapshots(
    session: AsyncSession, account_id: uuid.UUID, *, since: date
) -> list[YtChannelSnapshot]:
    result = await session.execute(
        select(YtChannelSnapshot)
        .where(
            YtChannelSnapshot.account_id == account_id,
            YtChannelSnapshot.snapshot_date >= since,
        )
        .order_by(YtChannelSnapshot.snapshot_date.asc())
    )
    return list(result.scalars().all())


async def upsert_videos(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    items: Sequence[dict[str, Any]],
) -> list[YtVideo]:
    now = utc_now()
    existing: dict[str, YtVideo] = {}
    ids = [item["video_id"] for item in items]
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        result = await session.execute(
            select(YtVideo).where(YtVideo.account_id == account_id, YtVideo.video_id.in_(chunk))
        )
        existing.update({video.video_id: video for video in result.scalars().all()})

    records: list[YtVideo] = []
    for item in items:
        video = existing.get(item["video_id"])
        if video is None:
            video = YtVideo(
                account_id=account_id,
                user_id=user_id,
                video_id=item["video_id"],
            )
            session.add(video)
        video.title = item["title"]
        video.description = item["description"]
        video.tags = item["tags"]
        video.category_id = item["category_id"]
        video.default_language = item["default_language"]
        video.published_at = item["published_at"]
        video.duration_seconds = item["duration_seconds"]
        video.is_short = item["is_short"]
        video.privacy_status = item["privacy_status"]
        video.thumbnail_url = item["thumbnail_url"]
        video.view_count = item["view_count"]
        video.like_count = item["like_count"]
        video.comment_count = item["comment_count"]
        video.engagement_rate = engagement_rate(
            item["like_count"], item["comment_count"], item["view_count"]
        )
        video.stats_synced_at = now
        records.append(video)
    await session.flush()
    return records


async def delete_videos_not_in(
    session: AsyncSession, account_id: uuid.UUID, keep_ids: Sequence[str]
) -> int:
    statement = delete(YtVideo).where(YtVideo.account_id == account_id)
    if keep_ids:
        statement = statement.where(YtVideo.video_id.not_in(list(keep_ids)))
    result = await session.execute(statement)
    return result.rowcount or 0


async def get_all_videos(session: AsyncSession, account_id: uuid.UUID) -> list[YtVideo]:
    result = await session.execute(
        select(YtVideo)
        .where(YtVideo.account_id == account_id)
        .order_by(YtVideo.published_at.desc())
    )
    return list(result.scalars().all())


async def get_video(
    session: AsyncSession, account_id: uuid.UUID, video_id: str
) -> Optional[YtVideo]:
    result = await session.execute(
        select(YtVideo).where(YtVideo.account_id == account_id, YtVideo.video_id == video_id)
    )
    return result.scalar_one_or_none()


async def get_videos_by_ids(
    session: AsyncSession, account_id: uuid.UUID, video_ids: Sequence[str]
) -> dict[str, YtVideo]:
    if not video_ids:
        return {}
    result = await session.execute(
        select(YtVideo).where(YtVideo.account_id == account_id, YtVideo.video_id.in_(list(video_ids)))
    )
    return {video.video_id: video for video in result.scalars().all()}


async def apply_performance_indexes(
    session: AsyncSession,
    videos: Sequence[YtVideo],
    indexes: dict[str, Optional[float]],
) -> None:
    for video in videos:
        video.performance_index = indexes.get(video.video_id)
    await session.flush()


def _sort_column(sort: VideoSort):
    if sort is VideoSort.VIEWS:
        return YtVideo.view_count
    if sort is VideoSort.LIKES:
        return func.coalesce(YtVideo.like_count, 0)
    if sort is VideoSort.COMMENTS:
        return func.coalesce(YtVideo.comment_count, 0)
    if sort is VideoSort.ENGAGEMENT:
        return YtVideo.engagement_rate
    if sort is VideoSort.PERFORMANCE:
        return func.coalesce(YtVideo.performance_index, -1)
    if sort is VideoSort.DURATION:
        return YtVideo.duration_seconds
    return YtVideo.published_at


async def list_videos(
    session: AsyncSession,
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
) -> tuple[list[YtVideo], int]:
    conditions = [YtVideo.account_id == account_id]
    if search:
        pattern = f"%{search.strip().lower()}%"
        conditions.append(func.lower(YtVideo.title).like(pattern))
    if content_type is ContentType.SHORT:
        conditions.append(YtVideo.is_short.is_(True))
    elif content_type is ContentType.LONG:
        conditions.append(YtVideo.is_short.is_(False))
    if privacy_status:
        conditions.append(YtVideo.privacy_status == privacy_status)
    if date_from is not None:
        conditions.append(YtVideo.published_at >= date_from)
    if date_to is not None:
        conditions.append(YtVideo.published_at <= date_to)

    total = (
        await session.execute(select(func.count()).select_from(YtVideo).where(*conditions))
    ).scalar_one()

    column = _sort_column(sort)
    ordering = column.asc() if order is SortOrder.ASC else column.desc()
    result = await session.execute(
        select(YtVideo)
        .where(*conditions)
        .order_by(ordering, YtVideo.published_at.desc(), YtVideo.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total)


async def get_video_aggregates(
    session: AsyncSession, account_ids: Sequence[uuid.UUID], *, since: datetime
) -> dict[uuid.UUID, dict[str, Any]]:
    if not account_ids:
        return {}
    result = await session.execute(
        select(
            YtVideo.account_id,
            func.count(YtVideo.id),
            func.coalesce(func.sum(YtVideo.view_count), 0),
            func.max(YtVideo.published_at),
            func.avg(YtVideo.performance_index),
            func.coalesce(func.sum(case((YtVideo.published_at >= since, 1), else_=0)), 0),
        )
        .where(YtVideo.account_id.in_(list(account_ids)))
        .group_by(YtVideo.account_id)
    )
    return {
        row[0]: {
            "video_count": int(row[1]),
            "total_video_views": int(row[2]),
            "last_published_at": row[3],
            "average_performance_index": float(row[4]) if row[4] is not None else None,
            "recent_uploads": int(row[5]),
        }
        for row in result.all()
    }


async def get_top_video(session: AsyncSession, account_id: uuid.UUID) -> Optional[YtVideo]:
    result = await session.execute(
        select(YtVideo)
        .where(YtVideo.account_id == account_id)
        .order_by(YtVideo.view_count.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_cache(
    session: AsyncSession, account_id: uuid.UUID, cache_key: str
) -> Optional[YtAnalyticsCache]:
    result = await session.execute(
        select(YtAnalyticsCache)
        .where(
            YtAnalyticsCache.account_id == account_id,
            YtAnalyticsCache.cache_key == cache_key,
            YtAnalyticsCache.expires_at > utc_now(),
        )
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def set_cache(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    cache_key: str,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> YtAnalyticsCache:
    now = utc_now()
    await session.execute(delete(YtAnalyticsCache).where(YtAnalyticsCache.expires_at <= now))
    result = await session.execute(
        select(YtAnalyticsCache).where(
            YtAnalyticsCache.account_id == account_id,
            YtAnalyticsCache.cache_key == cache_key,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        record = YtAnalyticsCache(account_id=account_id, cache_key=cache_key)
        session.add(record)
    record.payload = payload
    record.fetched_at = now
    record.expires_at = now + timedelta(seconds=ttl_seconds)
    await session.flush()
    return record


async def clear_account_cache(session: AsyncSession, account_id: uuid.UUID) -> None:
    await session.execute(delete(YtAnalyticsCache).where(YtAnalyticsCache.account_id == account_id))
# app/crud/yt_media_upload.py
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.yt_channel_management import YtUploadJob
from app.models.yt_media_upload import YtMediaAsset
from app.utils.yt_channel_management import UploadJobStatus
from app.utils.yt_media_upload import LIVE_MEDIA_STATUSES, MediaAssetStatus


async def create_asset(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    user_id: uuid.UUID,
    object_key: str,
    original_name: str,
    content_type: str,
    size_bytes: int,
    part_size_bytes: int,
    total_parts: int,
    upload_id: str,
    deadline: datetime,
) -> YtMediaAsset:
    asset = YtMediaAsset(
        id=asset_id,
        user_id=user_id,
        object_key=object_key,
        original_name=original_name,
        content_type=content_type,
        size_bytes=size_bytes,
        part_size_bytes=part_size_bytes,
        total_parts=total_parts,
        upload_id=upload_id,
        status=MediaAssetStatus.UPLOADING.value,
        upload_deadline=deadline,
    )
    session.add(asset)
    await session.flush()
    return asset


async def get_asset(
    session: AsyncSession, asset_id: uuid.UUID, *, lock: bool = False
) -> Optional[YtMediaAsset]:
    statement = (
        select(YtMediaAsset)
        .where(YtMediaAsset.id == asset_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalar_one_or_none()


async def get_job_for_asset(
    session: AsyncSession, asset_id: uuid.UUID
) -> Optional[YtUploadJob]:
    result = await session.execute(
        select(YtUploadJob)
        .where(YtUploadJob.media_asset_id == asset_id)
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def count_live_assets(session: AsyncSession, user_id: uuid.UUID) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(YtMediaAsset)
        .where(
            YtMediaAsset.user_id == user_id,
            YtMediaAsset.status.in_(LIVE_MEDIA_STATUSES),
        )
    )
    return int(total or 0)


async def sum_live_bytes(session: AsyncSession) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(YtMediaAsset.size_bytes), 0)).where(
            YtMediaAsset.status.in_(LIVE_MEDIA_STATUSES)
        )
    )
    return int(total or 0)


async def mark_ready(
    session: AsyncSession, asset: YtMediaAsset, *, now: datetime, expires_at: datetime
) -> None:
    asset.status = MediaAssetStatus.READY.value
    asset.ready_at = now
    asset.expires_at = expires_at
    asset.upload_id = None
    asset.upload_deadline = None
    await session.flush()


async def mark_deleted(session: AsyncSession, asset: YtMediaAsset, *, now: datetime) -> None:
    asset.status = MediaAssetStatus.DELETED.value
    asset.deleted_at = now
    asset.upload_id = None
    await session.flush()


async def mark_aborted(session: AsyncSession, asset: YtMediaAsset, *, now: datetime) -> None:
    asset.status = MediaAssetStatus.ABORTED.value
    asset.deleted_at = now
    asset.upload_id = None
    await session.flush()


async def list_due_for_abort(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[YtMediaAsset]:
    result = await session.execute(
        select(YtMediaAsset)
        .where(
            YtMediaAsset.status == MediaAssetStatus.UPLOADING.value,
            YtMediaAsset.upload_deadline <= now,
        )
        .order_by(YtMediaAsset.upload_deadline)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_due_for_expiry(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[YtMediaAsset]:
    result = await session.execute(
        select(YtMediaAsset)
        .where(
            YtMediaAsset.status == MediaAssetStatus.READY.value,
            YtMediaAsset.expires_at <= now,
        )
        .order_by(YtMediaAsset.expires_at)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_finished_ready(session: AsyncSession, *, limit: int) -> list[YtMediaAsset]:
    result = await session.execute(
        select(YtMediaAsset)
        .join(YtUploadJob, YtUploadJob.media_asset_id == YtMediaAsset.id)
        .where(
            YtMediaAsset.status == MediaAssetStatus.READY.value,
            YtUploadJob.status.in_(
                (UploadJobStatus.COMPLETED.value, UploadJobStatus.CANCELLED.value)
            ),
        )
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_assets_by_ids(
    session: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, YtMediaAsset]:
    if not asset_ids:
        return {}
    result = await session.execute(
        select(YtMediaAsset)
        .where(YtMediaAsset.id.in_(asset_ids))
        .execution_options(populate_existing=True)
    )
    return {asset.id: asset for asset in result.scalars().all()}
# app/service/yt_media_upload.py
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.core.exceptions import (
    AppException,
    ConflictError,
    MediaExpiredError,
    NotFoundError,
    StagingBusyError,
    UploadIncompleteError,
    UploadSessionGoneError,
    ValidationError,
)
from app.crud import yt_channel_management as job_crud
from app.crud.yt_channel_management import count_active_targets as _count_active_targets
from app.crud import yt_media_upload as crud
from app.models.yt_media_upload import YtMediaAsset
from app.schemas.yt_media_upload import DeclaredFile, MediaUploadInfo, PartUrl
from app.utils import bucket
from app.utils.datetime_utils import ensure_utc, utc_now
from app.utils.yt_media_upload import (
    MediaAssetStatus,
    build_object_key,
    expected_part_size,
    file_extension,
    find_part_problems,
    looks_like_video,
    retention_deadline,
    total_parts,
    upload_deadline,
)

logger = logging.getLogger(__name__)

_INVALID_VIDEO = (
    "The uploaded file does not look like a valid video. "
    "Please choose an MP4, MOV, AVI, MKV, WEBM or MPEG file."
)


@dataclass
class ReaperStats:
    aborted: int = 0
    expired: int = 0
    finished: int = 0
    repaired: int = 0


class YtMediaUploadService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start_session(self, user_id: uuid.UUID, file: DeclaredFile) -> YtMediaAsset:
        await bucket.ensure_bucket()
        if await crud.count_live_assets(self._session, user_id) >= settings.media_max_active_per_user:
            raise ConflictError(
                f"You already have {settings.media_max_active_per_user} videos waiting to be "
                "published. Wait for them to finish or cancel one, then try again.",
                code="too_many_uploads",
            )
        staged = await crud.sum_live_bytes(self._session)
        if staged + file.size_bytes > settings.media_max_staged_bytes_total:
            logger.warning("Staging capacity reached (%s bytes in use)", staged)
            raise StagingBusyError()

        asset_id = uuid.uuid4()
        object_key = build_object_key(user_id, asset_id, file_extension(file.name))
        upload_id = await bucket.create_multipart_upload(object_key, file.content_type or "")
        try:
            return await crud.create_asset(
                self._session,
                asset_id=asset_id,
                user_id=user_id,
                object_key=object_key,
                original_name=file.name,
                content_type=file.content_type or "",
                size_bytes=file.size_bytes,
                part_size_bytes=settings.media_part_size_bytes,
                total_parts=total_parts(file.size_bytes, settings.media_part_size_bytes),
                upload_id=upload_id,
                deadline=upload_deadline(utc_now()),
            )
        except Exception:
            await self.abort_quietly(object_key, upload_id)
            raise

    async def abort_quietly(self, object_key: str, upload_id: Optional[str]) -> None:
        if not upload_id:
            return
        try:
            await bucket.abort_multipart_upload(object_key, upload_id)
        except AppException:
            logger.warning("Could not abort the upload session of %s", object_key)

    async def upload_info(
        self, asset: YtMediaAsset, *, known_parts: Optional[list[bucket.PartInfo]] = None
    ) -> Optional[MediaUploadInfo]:
        if asset.status != MediaAssetStatus.UPLOADING.value or not asset.upload_id:
            return None
        parts = known_parts
        if parts is None:
            parts = await bucket.list_parts(asset.object_key, asset.upload_id)
        done = {
            part.part_number
            for part in parts
            if part.size == expected_part_size(part.part_number, asset.size_bytes, asset.part_size_bytes)
        }
        pending = [n for n in range(1, asset.total_parts + 1) if n not in done]
        urls = await bucket.create_part_urls(asset.object_key, asset.upload_id, pending)
        return MediaUploadInfo(
            asset_id=asset.id,
            status=MediaAssetStatus(asset.status),
            size_bytes=asset.size_bytes,
            part_size_bytes=asset.part_size_bytes,
            total_parts=asset.total_parts,
            upload_deadline=asset.upload_deadline,
            uploaded_part_numbers=sorted(done),
            parts=[
                PartUrl(
                    part_number=number,
                    size_bytes=expected_part_size(number, asset.size_bytes, asset.part_size_bytes),
                    url=urls[number],
                )
                for number in pending
            ],
        )

    async def finalize(self, asset_id: uuid.UUID) -> YtMediaAsset:
        asset = await crud.get_asset(self._session, asset_id, lock=True)
        if asset is None:
            raise NotFoundError("The video upload was not found.")
        if asset.status == MediaAssetStatus.READY.value:
            return asset
        if asset.status != MediaAssetStatus.UPLOADING.value:
            raise MediaExpiredError()

        now = utc_now()
        if asset.upload_deadline is not None and now > ensure_utc(asset.upload_deadline):
            raise UploadSessionGoneError()

        await self._join_parts(asset)

        info = await bucket.head_object(asset.object_key)
        if info is None or info.size != asset.size_bytes:
            raise UploadIncompleteError(
                "The stored file does not match the size of the selected video. "
                "Please upload it again."
            )

        header = await bucket.read_range(
            asset.object_key, 0, settings.media_signature_probe_bytes
        )
        if not looks_like_video(header):
            await self._reject_invalid(asset, now)
            raise ValidationError(_INVALID_VIDEO, code="invalid_video_file")

        await crud.mark_ready(
            self._session, asset, now=now, expires_at=retention_deadline(now)
        )
        logger.info("Media asset %s is ready (%s bytes)", asset.id, asset.size_bytes)
        return asset

    async def _join_parts(self, asset: YtMediaAsset) -> None:
        try:
            parts = await bucket.list_parts(asset.object_key, asset.upload_id or "")
        except UploadSessionGoneError:
            if await bucket.head_object(asset.object_key) is not None:
                return
            raise
        missing, wrong = find_part_problems(parts, asset.size_bytes, asset.part_size_bytes)
        if missing or wrong:
            details = []
            if missing:
                details.append(
                    {"field": "parts", "message": f"{len(missing)} parts are still missing."}
                )
            if wrong:
                details.append(
                    {"field": "parts", "message": f"{len(wrong)} parts have the wrong size."}
                )
            raise UploadIncompleteError(details=details)
        await bucket.complete_multipart_upload(asset.object_key, asset.upload_id or "", parts)

    async def _reject_invalid(self, asset: YtMediaAsset, now: datetime) -> None:
        try:
            await bucket.delete_object(asset.object_key)
        except AppException:
            logger.warning("Could not delete the rejected file %s", asset.object_key)
        await crud.mark_aborted(self._session, asset, now=now)
        job = await crud.get_job_for_asset(self._session, asset.id)
        if job is not None:
            await job_crud.fail_job_targets(
                self._session,
                job.id,
                code="invalid_video_file",
                message=_INVALID_VIDEO,
                now=now,
            )
        await self._session.commit()
        if job is not None:
            await job_crud.refresh_job_status(self._session, job.id)
            await self._session.commit()

    async def discard(self, asset_id: uuid.UUID) -> bool:
        asset = await crud.get_asset(self._session, asset_id, lock=True)
        if asset is None:
            return False
        now = utc_now()
        if asset.status == MediaAssetStatus.UPLOADING.value:
            if asset.upload_id:
                await bucket.abort_multipart_upload(asset.object_key, asset.upload_id)
            await crud.mark_aborted(self._session, asset, now=now)
            return True
        if asset.status == MediaAssetStatus.READY.value:
            await bucket.delete_object(asset.object_key)
            await crud.mark_deleted(self._session, asset, now=now)
            return True
        return False


async def run_reaper_once(
    session_factory: async_sessionmaker[AsyncSession], *, now: Optional[datetime] = None
) -> ReaperStats:
    now = now or utc_now()
    stats = ReaperStats()
    limit = settings.media_reaper_batch_size

    touched: set = set()

    async with session_factory() as session:
        for asset in await crud.list_due_for_abort(session, now=now, limit=limit):
            try:
                if asset.upload_id:
                    await bucket.abort_multipart_upload(asset.object_key, asset.upload_id)
            except AppException:
                logger.warning("Could not abort abandoned upload %s, will retry", asset.id)
                continue
            await crud.mark_aborted(session, asset, now=now)
            job = await crud.get_job_for_asset(session, asset.id)
            if job is not None:
                await job_crud.fail_job_targets(
                    session,
                    job.id,
                    code="media_upload_incomplete",
                    message="The video upload was not finished in time. Please start again.",
                    now=now,
                )
                touched.add(job.id)
            stats.aborted += 1
        await session.commit()

    async with session_factory() as session:
        for asset in await crud.list_due_for_expiry(session, now=now, limit=limit):
            job = await crud.get_job_for_asset(session, asset.id)
            # Skip if any channel still needs this file: pending, uploading, or retrying.
            # Using only count_uploading_targets was wrong — pending channels also need the
            # file but haven't been claimed by a worker yet.
            if job is not None and await _count_active_targets(session, job.id):
                continue
            try:
                await bucket.delete_object(asset.object_key)
            except AppException:
                logger.warning("Could not delete expired file %s, will retry", asset.id)
                continue
            await crud.mark_deleted(session, asset, now=now)
            if job is not None:
                failed = await job_crud.fail_job_targets(
                    session,
                    job.id,
                    code="media_expired",
                    message=(
                        "The video file was removed after 24 hours. "
                        "Start a new upload to publish it again."
                    ),
                    now=now,
                )
                if failed:
                    touched.add(job.id)
            stats.expired += 1
        await session.commit()

    if touched:
        async with session_factory() as session:
            for job_id in sorted(touched):
                await job_crud.refresh_job_status(session, job_id)
            await session.commit()

    async with session_factory() as session:
        stuck = await job_crud.list_stuck_job_ids(session, limit=limit)
        for job_id in sorted(stuck):
            await job_crud.refresh_job_status(session, job_id)
        await session.commit()
        stats.repaired += len(stuck)

    async with session_factory() as session:
        for asset in await crud.list_finished_ready(session, limit=limit):
            try:
                await bucket.delete_object(asset.object_key)
            except AppException:
                logger.warning("Could not delete finished file %s, will retry", asset.id)
                continue
            await crud.mark_deleted(session, asset, now=now)
            stats.finished += 1
        await session.commit()

    if stats.aborted or stats.expired or stats.finished or stats.repaired:
        logger.info(
            "Media cleanup: %s abandoned, %s expired, %s finished files removed, %s jobs repaired",
            stats.aborted,
            stats.expired,
            stats.finished,
            stats.repaired,
        )
    return stats
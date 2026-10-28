# app/service/yt_channel_management.py
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    AppException,
    MediaExpiredError,
    NotFoundError,
    ValidationError,
)
from app.crud import storage_bucket as storage_crud
from app.crud import yt_channel_management as crud
from app.crud import yt_media_upload as media_crud
from app.models.storage_bucket import StorageFile, StorageFileType
from app.models.yt_channel_management import YtUploadJob, YtUploadTarget
from app.models.yt_media_upload import YtMediaAsset
from app.schemas.yt_channel_management import (
    CreateUploadRequest,
    StartUploadResponse,
    UploadChannelBrief,
    UploadJobDetail,
    UploadJobListItem,
    UploadJobListResponse,
    UploadQuotaResponse,
    UploadTargetResponse,
)
from app.schemas.yt_media_upload import MediaStateResponse
from app.service.google_credentials import ensure_configured
from app.service.yt_media_upload import YtMediaUploadService
from app.utils.datetime_utils import ensure_utc, utc_now
from app.utils.yt_channel_management import (
    ACTIVE_TARGET_STATUSES,
    UploadJobStatus,
    UploadTargetStatus,
    next_quota_reset,
    quota_day,
)
from app.utils.yt_media_upload import MediaAssetStatus

logger = logging.getLogger(__name__)


def _summary_data(job: YtUploadJob, asset: Optional[YtMediaAsset]) -> dict:
    counts = {status.value: 0 for status in UploadTargetStatus}
    for target in job.targets:
        counts[target.status] = counts.get(target.status, 0) + 1
    return {
        "id": job.id,
        "status": job.status,
        "title": job.title,
        "privacy_status": job.privacy_status,
        "media_status": asset.status if asset is not None else None,
        "media_expires_at": asset.expires_at if asset is not None else None,
        "source_file_name": job.source_file_name,
        "source_size_bytes": job.source_size_bytes,
        "total_channels": len(job.targets),
        "done_count": counts[UploadTargetStatus.DONE.value],
        "failed_count": counts[UploadTargetStatus.FAILED.value],
        "cancelled_count": counts[UploadTargetStatus.CANCELLED.value],
        "active_count": sum(counts[value] for value in ACTIVE_TARGET_STATUSES),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _to_list_item(
    job: YtUploadJob,
    emails: dict[uuid.UUID, Optional[str]],
    assets: dict[uuid.UUID, YtMediaAsset],
) -> UploadJobListItem:
    return UploadJobListItem(
        **_summary_data(job, assets.get(job.media_asset_id)),
        channels=[
            UploadChannelBrief(
                account_id=target.account_id,
                account_nickname=target.account_nickname,
                account_email=emails.get(target.account_id),
                channel_title=target.channel_title,
                status=target.status,
                youtube_url=target.youtube_url,
            )
            for target in job.targets
        ],
    )


def _to_target(
    target: YtUploadTarget, emails: dict[uuid.UUID, Optional[str]]
) -> UploadTargetResponse:
    response = UploadTargetResponse.model_validate(target)
    response.account_email = emails.get(target.account_id)
    return response


def _to_detail(
    job: YtUploadJob,
    emails: dict[uuid.UUID, Optional[str]],
    asset: Optional[YtMediaAsset],
) -> UploadJobDetail:
    return UploadJobDetail(
        **_summary_data(job, asset),
        description=job.description,
        tags=list(job.tags or []),
        category_id=job.category_id,
        publish_at=job.publish_at,
        made_for_kids=job.made_for_kids,
        default_language=job.default_language,
        notify_subscribers=job.notify_subscribers,
        source_content_type=job.source_content_type,
        thumbnail_file_id=job.thumbnail_file_id,
        thumbnail_file_name=job.thumbnail_file_name,
        targets=[_to_target(target, emails) for target in job.targets],
    )


class YtChannelManagementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._media = YtMediaUploadService(session)

    async def create_upload(
        self, user_id: uuid.UUID, payload: CreateUploadRequest
    ) -> StartUploadResponse:
        await ensure_configured(self._session, user_id)

        if payload.client_request_id:
            existing = await crud.get_job_by_request(
                self._session, user_id, payload.client_request_id
            )
            if existing is not None:
                return await self._reuse(existing)

        thumbnail = await self._get_thumbnail(user_id, payload.thumbnail_file_id)
        accounts = await self._get_uploadable_accounts(user_id, payload.account_ids)
        await self._ensure_quota(user_id, len(accounts))

        asset = await self._media.start_session(user_id, payload.file)
        try:
            job = await crud.create_job(
                self._session,
                user_id=user_id,
                asset=asset,
                client_request_id=payload.client_request_id,
                thumbnail_file=thumbnail,
                title=payload.title,
                description=payload.description,
                tags=payload.tags,
                category_id=payload.category_id,
                privacy_status=payload.privacy_status.value,
                publish_at=payload.publish_at,
                made_for_kids=payload.made_for_kids,
                default_language=payload.default_language,
                notify_subscribers=payload.notify_subscribers,
                accounts=[accounts[account_id] for account_id in payload.account_ids],
            )
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            await self._media.abort_quietly(asset.object_key, asset.upload_id)
            if payload.client_request_id:
                existing = await crud.get_job_by_request(
                    self._session, user_id, payload.client_request_id
                )
                if existing is not None:
                    return await self._reuse(existing)
            raise
        except Exception:
            await self._session.rollback()
            await self._media.abort_quietly(asset.object_key, asset.upload_id)
            raise

        logger.info(
            "Upload job %s created by user %s for %s channels (%s bytes)",
            job.id,
            user_id,
            len(job.targets),
            asset.size_bytes,
        )
        info = await self._media.upload_info(asset, known_parts=[])
        return StartUploadResponse(
            job=_to_detail(job, await self._load_emails([job]), asset), upload=info
        )

    async def _reuse(self, job: YtUploadJob) -> StartUploadResponse:
        asset = await self._asset_of(job)
        info = None
        if asset is not None and asset.status == MediaAssetStatus.UPLOADING.value:
            info = await self._media.upload_info(asset)
        return StartUploadResponse(
            job=_to_detail(job, await self._load_emails([job]), asset),
            upload=info,
            reused=True,
        )

    async def get_media_state(
        self, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> MediaStateResponse:
        job = await self._get_job(user_id, job_id)
        asset = await self._asset_of(job)
        if asset is None:
            return MediaStateResponse(job_id=job.id)
        info = await self._media.upload_info(asset)
        return MediaStateResponse(
            job_id=job.id,
            media_status=asset.status,
            media_expires_at=asset.expires_at,
            upload=info,
        )

    async def complete_media(
        self, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> UploadJobDetail:
        job = await self._get_job(user_id, job_id)
        if job.media_asset_id is None:
            raise MediaExpiredError()
        if job.status == UploadJobStatus.CANCELLED.value:
            raise ValidationError("This upload was cancelled.")
        asset = await self._media.finalize(job.media_asset_id)
        await self._session.commit()
        await crud.refresh_job_status(self._session, job.id)
        await self._session.commit()
        logger.info("Upload job %s media is ready", job.id)
        fresh = await self._get_job(user_id, job_id)
        return _to_detail(fresh, await self._load_emails([fresh]), asset)

    async def _load_emails(
        self, jobs: list[YtUploadJob]
    ) -> dict[uuid.UUID, Optional[str]]:
        account_ids = {
            target.account_id
            for job in jobs
            for target in job.targets
            if target.account_id is not None
        }
        return await crud.get_account_emails(self._session, list(account_ids))

    async def _asset_of(self, job: YtUploadJob) -> Optional[YtMediaAsset]:
        if job.media_asset_id is None:
            return None
        return await media_crud.get_asset(self._session, job.media_asset_id)

    async def list_uploads(
        self,
        user_id: uuid.UUID,
        *,
        status: Optional[UploadJobStatus],
        account_id: Optional[uuid.UUID],
        search: Optional[str],
        date_from: Optional[datetime],
        date_to: Optional[datetime],
        limit: int,
        offset: int,
    ) -> UploadJobListResponse:
        jobs, total = await crud.list_jobs(
            self._session,
            user_id=user_id,
            status=status,
            account_id=account_id,
            search=(search or "").strip() or None,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        emails = await self._load_emails(list(jobs))
        assets = await media_crud.get_assets_by_ids(
            self._session, [job.media_asset_id for job in jobs if job.media_asset_id]
        )
        return UploadJobListResponse(
            jobs=[_to_list_item(job, emails, assets) for job in jobs],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_upload(self, user_id: uuid.UUID, job_id: uuid.UUID) -> UploadJobDetail:
        job = await self._get_job(user_id, job_id)
        return _to_detail(job, await self._load_emails([job]), await self._asset_of(job))

    async def retry_upload(self, user_id: uuid.UUID, job_id: uuid.UUID) -> UploadJobDetail:
        await ensure_configured(self._session, user_id)
        job = await self._get_job(user_id, job_id)
        failed = [
            target
            for target in job.targets
            if target.status == UploadTargetStatus.FAILED.value
        ]
        if not failed:
            raise ValidationError("There are no failed channels to retry.")

        asset = await self._asset_of(job)
        if (
            asset is None
            or asset.status != MediaAssetStatus.READY.value
            or (asset.expires_at is not None and ensure_utc(asset.expires_at) <= utc_now())
        ):
            raise MediaExpiredError()

        accounts = await crud.get_accounts_by_ids(
            self._session,
            [target.account_id for target in failed if target.account_id is not None],
            user_id,
        )
        retryable = [
            target
            for target in failed
            if target.account_id in accounts
            and accounts[target.account_id].is_active
            and not accounts[target.account_id].needs_reconnect
        ]
        if not retryable:
            raise ValidationError(
                "The failed channels need to be reconnected before they can be retried."
            )
        await self._ensure_quota(user_id, len(retryable))

        await crud.reset_targets(
            self._session,
            [target.id for target in retryable],
            has_thumbnail=job.thumbnail_file_id is not None,
        )
        await self._session.commit()
        await crud.refresh_job_status(self._session, job.id)
        await self._session.commit()
        logger.info("Retrying %s channels of upload job %s", len(retryable), job.id)
        fresh = await self._get_job(user_id, job_id)
        return _to_detail(fresh, await self._load_emails([fresh]), asset)

    async def cancel_upload(self, user_id: uuid.UUID, job_id: uuid.UUID) -> UploadJobDetail:
        job = await self._get_job(user_id, job_id)
        cancelled = await crud.cancel_active_targets(
            self._session, job.id, now=utc_now()
        )
        if cancelled == 0:
            raise ValidationError("This upload has already finished.")
        await self._session.commit()
        new_status = await crud.refresh_job_status(self._session, job.id)
        await self._session.commit()
        logger.info("Upload job %s cancelled (%s channels)", job.id, cancelled)

        if new_status is UploadJobStatus.CANCELLED and job.media_asset_id is not None:
            await self._discard_quietly(job.media_asset_id)

        fresh = await self._get_job(user_id, job_id)
        return _to_detail(fresh, await self._load_emails([fresh]), await self._asset_of(fresh))

    async def _discard_quietly(self, asset_id: uuid.UUID) -> None:
        try:
            await self._media.discard(asset_id)
            await self._session.commit()
        except AppException:
            await self._session.rollback()
            logger.warning("Could not remove the file of cancelled upload (asset %s)", asset_id)

    async def get_quota(self, user_id: uuid.UUID) -> UploadQuotaResponse:
        now = utc_now()
        used = await crud.get_quota_calls(self._session, user_id, quota_day(now))
        waiting = await crud.count_waiting_targets(self._session, user_id)
        limit = settings.yt_upload_daily_limit
        return UploadQuotaResponse(
            daily_limit=limit,
            used_today=used,
            waiting=waiting,
            remaining=max(limit - used - waiting, 0),
            resets_at=next_quota_reset(now),
        )

    async def _get_job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> YtUploadJob:
        job = await crud.get_job(self._session, job_id, user_id)
        if job is None:
            raise NotFoundError("Upload not found.")
        return job

    async def _get_thumbnail(
        self, user_id: uuid.UUID, file_id: Optional[uuid.UUID]
    ) -> Optional[StorageFile]:
        if file_id is None:
            return None
        record = await storage_crud.get_file(self._session, file_id, user_id)
        if record is None:
            raise NotFoundError("The selected thumbnail was not found in your library.")
        if (
            record.file_type != StorageFileType.IMAGE.value
            or record.content_type not in settings.yt_thumbnail_content_types
        ):
            raise ValidationError("The thumbnail must be a JPG or PNG image from your library.")
        return record

    async def _get_uploadable_accounts(
        self, user_id: uuid.UUID, account_ids: list[uuid.UUID]
    ) -> dict:
        accounts = await crud.get_accounts_by_ids(self._session, account_ids, user_id)
        missing = [
            account_id
            for account_id in account_ids
            if account_id not in accounts or not accounts[account_id].is_active
        ]
        if missing:
            raise NotFoundError(
                "Some of the selected YouTube channels were not found or are disconnected."
            )
        needs_reconnect = [
            accounts[account_id].nickname
            for account_id in account_ids
            if accounts[account_id].needs_reconnect
        ]
        if needs_reconnect:
            raise ValidationError(
                "Reconnect these channels before uploading: " + ", ".join(needs_reconnect) + "."
            )
        return accounts

    async def _ensure_quota(self, user_id: uuid.UUID, needed: int) -> None:
        quota = await self.get_quota(user_id)
        if needed > quota.remaining:
            raise ValidationError(
                f"Only {quota.remaining} uploads are left for today in your Google project "
                f"(daily limit {quota.daily_limit}). Select fewer channels or try again "
                "after the limit resets at midnight Pacific Time."
            )
# app/service/yt_channel_management.py
import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from typing import Optional

from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    AppException,
    MediaExpiredError,
    NotFoundError,
    ValidationError,
    AiNotConfiguredError,
    AiGenerationError,
)
from app.crud import storage_bucket as storage_crud
from app.crud import yt_channel_management as crud
from app.crud import yt_media_upload as media_crud
from app.integrations import youtube_api
from app.integrations.gemini_api import GeminiError, generate_json_sync
from app.models.storage_bucket import StorageFile, StorageFileType
from app.models.yt_channel_management import YtUploadJob, YtUploadTarget
from app.models.yt_media_upload import YtMediaAsset
from app.schemas.yt_channel_management import (
    CreateUploadRequest,
    DirectUploadRequest,
    GenerateChannelMetadataRequest,
    GenerateChannelMetadataResponse,
    GeneratedChannelMetadata,
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
from app.service.yt_management import YouTubeManagementService
from app.service.yt_media_upload import YtMediaUploadService
from app.utils import bucket
from app.utils.datetime_utils import ensure_utc, utc_now
from app.utils.upload_resources import calculate_parallel_upload_limit
from app.utils.yt_ai_insights import parse_model_json
from app.utils.yt_channel_management import (
    ACTIVE_TARGET_STATUSES,
    UploadJobStatus,
    UploadTargetStatus,
    ThumbnailStatus,
    body_options_from_job,
    build_video_body,
    classify_error,
    watch_url,
    next_quota_reset,
    quota_day,
)
from app.utils.yt_media_upload import (
    MediaAssetStatus,
    looks_like_video,
    resolve_video_content_type,
    check_video_size,
)

logger = logging.getLogger(__name__)


def _channel_metadata_map(payload) -> dict[uuid.UUID, dict]:
    return {
        item.account_id: {
            "title": item.title,
            "description": item.description,
            "tags": list(item.tags or []),
        }
        for item in payload.channel_metadata
    }


def _video_options(payload) -> dict:
    """Optional video settings from the request. None means "not chosen"."""
    return {
        "default_audio_language": payload.default_audio_language,
        "contains_synthetic_media": payload.contains_synthetic_media,
        "license": payload.license.value if payload.license is not None else None,
        "embeddable": payload.embeddable,
        "public_stats_viewable": payload.public_stats_viewable,
    }


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
        "parallel_upload_limit": getattr(job, "parallel_upload_limit", 1),
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
        default_audio_language=job.default_audio_language,
        contains_synthetic_media=job.contains_synthetic_media,
        license=job.license,
        embeddable=job.embeddable,
        public_stats_viewable=job.public_stats_viewable,
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
            capacity = calculate_parallel_upload_limit(
                asset.size_bytes,
                len(payload.account_ids),
            )
            parallel_upload_limit = capacity.limit

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
                channel_metadata=_channel_metadata_map(payload),
                parallel_upload_limit=parallel_upload_limit,
                video_options=_video_options(payload),
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

    async def generate_channel_metadata(
        self,
        user_id: uuid.UUID,
        payload: GenerateChannelMetadataRequest,
    ) -> GenerateChannelMetadataResponse:
        accounts = await self._get_uploadable_accounts(user_id, payload.account_ids)
        if not settings.gemini_configured:
            raise AiNotConfiguredError()

        channel_context = [
            {
                "account_id": str(account_id),
                "nickname": accounts[account_id].nickname,
                "channel_title": accounts[account_id].channel_title,
            }
            for account_id in payload.account_ids
        ]
        system_prompt = (
            "You are a world-class YouTube metadata strategist and viral copywriter. "
            "Create unique, channel-specific metadata that feels native to that channel, "
            "uses natural curiosity and a strong hook, and avoids clickbait deception. "
            "Titles must be specific, readable, high-energy and 100 characters or fewer. "
            "Descriptions must add context, excitement and viewer value, and be 5000 bytes or fewer. "
            "Tags must be highly relevant, discoverable and not repetitive spam. "
            "Never mention that the text was AI-generated. Do not use < or >. "
            "Return only valid JSON."
        )
        user_prompt = (
            "MASTER METADATA:\n"
            f"Title: {payload.title}\n"
            f"Description: {payload.description or '(empty)'}\n"
            f"Tags: {', '.join(payload.tags)}\n\n"
            "SELECTED CHANNELS:\n"
            + "\n".join(
                f"- account_id={item['account_id']}; nickname={item['nickname']}; "
                f"channel_title={item['channel_title']}"
                for item in channel_context
            )
            + "\n\nRequired JSON shape:\n"
            '{"channels":[{"account_id":"UUID","title":"...","description":"...","tags":["..."]}]}'
            "\nRules: Create one entry per account_id. Preserve the meaning of the master metadata. "
            "Make each title distinct, opening-hook strong, and optimized for curiosity plus clarity. "
            "Descriptions should follow this structure: 1-2 high-impact lines, short context, "
            "3-6 useful takeaway bullets or short sections, a clear call to subscribe, and one natural CTA. "
            "Use only 8-15 relevant tags, avoid channel-name repetition unless useful for discovery."
        )

        try:
            text = await asyncio.to_thread(
                generate_json_sync,
                model=settings.gemini_model_fast,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            data = parse_model_json(text)
            response = GenerateChannelMetadataResponse.model_validate(data)
        except GeminiError as exc:
            if exc.kind == "not_configured":
                raise AiNotConfiguredError() from exc
            raise AiGenerationError() from exc
        except Exception as exc:
            raise AiGenerationError() from exc

        by_id = {item.account_id: item for item in response.channels}
        return GenerateChannelMetadataResponse(
            channels=[
                by_id.get(
                    account_id,
                    GeneratedChannelMetadata(
                        account_id=account_id,
                        title=payload.title,
                        description=payload.description,
                        tags=payload.tags,
                    ),
                )
                for account_id in payload.account_ids
            ]
        )

    async def direct_upload(
        self,
        user_id: uuid.UUID,
        payload: DirectUploadRequest,
        upload: UploadFile,
    ) -> UploadJobDetail:
        await ensure_configured(self._session, user_id)

        if payload.client_request_id:
            existing = await crud.get_job_by_request(
                self._session, user_id, payload.client_request_id
            )
            if existing is not None:
                return _to_detail(
                    existing,
                    await self._load_emails([existing]),
                    await self._asset_of(existing),
                )

        thumbnail = await self._get_thumbnail(user_id, payload.thumbnail_file_id)
        accounts = await self._get_uploadable_accounts(user_id, payload.account_ids)
        await self._ensure_quota(user_id, len(accounts))

        original_name = os.path.basename(upload.filename or "video.mp4")
        content_type = resolve_video_content_type(original_name, upload.content_type)
        upload.file.seek(0, os.SEEK_END)
        size = upload.file.tell()
        upload.file.seek(0)
        check_video_size(size)
        header = upload.file.read(32)
        upload.file.seek(0)
        if not looks_like_video(header):
            raise ValidationError("The uploaded file is not a supported valid video.")

        temp_dir = tempfile.mkdtemp(prefix="direct-yt-upload-")
        temp_path = os.path.join(temp_dir, original_name)
        try:
            with open(temp_path, "wb") as target:
                shutil.copyfileobj(upload.file, target)

            capacity = calculate_parallel_upload_limit(
                size,
                len(payload.account_ids),
            )
            parallel_upload_limit = capacity.limit
            logger.info(
                "Adaptive upload limit for job: %s channels concurrently (%s)",
                parallel_upload_limit,
                capacity.reason,
            )

            job = await crud.create_direct_job(
                self._session,
                user_id=user_id,
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
                source_file_name=original_name,
                source_content_type=content_type,
                source_size_bytes=size,
                parallel_upload_limit=parallel_upload_limit,
                accounts=[accounts[account_id] for account_id in payload.account_ids],
                channel_metadata=_channel_metadata_map(payload),
                video_options=_video_options(payload),
            )
            await self._session.commit()

            pending_targets = list(job.targets)
            active_tasks: dict[asyncio.Task, YtUploadTarget] = {}

            loop = asyncio.get_running_loop()

            async def run_target(target: YtUploadTarget):
                body = build_video_body(
                    title=(target.title or job.title).strip(),
                    description=(target.description or job.description).strip(),
                    tags=list(target.tags or job.tags or []),
                    category_id=job.category_id,
                    privacy_status=job.privacy_status,
                    publish_at=job.publish_at,
                    made_for_kids=job.made_for_kids,
                    default_language=job.default_language,
                    **body_options_from_job(job),
                )

                credentials = None
                for attempt in (1, 2):
                    try:
                        from app.core.database import SessionFactory
                        async with SessionFactory() as target_session:
                            credentials = await YouTubeManagementService(
                                target_session
                            ).get_fresh_credentials(user_id, target.account_id)

                        last_logged_percent = {"value": -1}

                        def on_direct_progress(uploaded: int, total: int) -> None:
                            percent = min(int(uploaded * 100 / max(total, 1)), 99)
                            if percent == last_logged_percent["value"]:
                                return
                            last_logged_percent["value"] = percent

                            def update_progress() -> None:
                                target.progress_percent = percent
                                target.bytes_confirmed = uploaded

                            loop.call_soon_threadsafe(update_progress)
                            logger.info(
                                "UPLOAD_PROGRESS job=%s channel=%s target=%s %d%% (%d/%d bytes)",
                                job.id,
                                target.channel_title,
                                target.id,
                                percent,
                                uploaded,
                                total,
                            )

                        logger.info(
                            "UPLOAD_START job=%s channel=%s target=%s parallel_limit=%d",
                            job.id,
                            target.channel_title,
                            target.id,
                            parallel_upload_limit,
                        )
                        video_id = await asyncio.to_thread(
                            youtube_api.upload_local_file_sync,
                            credentials,
                            file_path=temp_path,
                            content_type=content_type,
                            body=body,
                            notify_subscribers=payload.notify_subscribers,
                            on_progress=on_direct_progress,
                        )
                        return video_id, credentials, attempt, None
                    except Exception as exc:
                        failure = classify_error(exc)
                        if attempt == 2:
                            return None, credentials, attempt, failure

                return None, credentials, 2, classify_error(
                    RuntimeError("Upload retry failed")
                )

            while pending_targets or active_tasks:
                while pending_targets and len(active_tasks) < parallel_upload_limit:
                    target = pending_targets.pop(0)
                    target.status = UploadTargetStatus.UPLOADING.value
                    target.attempts = max(1, target.attempts + 1)
                    target.started_at = target.started_at or utc_now()
                    active_tasks[asyncio.create_task(run_target(target))] = target

                await self._session.commit()

                if not active_tasks:
                    break

                done, _ = await asyncio.wait(
                    active_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    target = active_tasks.pop(task)
                    video_id, credentials, attempts, failure = task.result()
                    target.attempts = attempts

                    if failure is not None or video_id is None:
                        logger.warning(
                            "UPLOAD_FAILED job=%s channel=%s target=%s code=%s message=%s",
                            job.id,
                            target.channel_title,
                            target.id,
                            failure.code if failure is not None else "no_video_id",
                            failure.message
                            if failure is not None
                            else "YouTube did not return a video ID",
                        )
                        await crud.mark_target_failed(
                            self._session,
                            target,
                            code=failure.code
                            if failure is not None
                            else "no_video_id",
                            message=failure.message
                            if failure is not None
                            else "YouTube did not return a video ID",
                            now=utc_now(),
                        )
                    else:
                        target.status = UploadTargetStatus.DONE.value
                        target.progress_percent = 100
                        target.bytes_confirmed = size
                        target.youtube_video_id = video_id
                        target.youtube_url = watch_url(video_id)
                        target.error_code = None
                        target.error_message = None
                        target.finished_at = utc_now()

                        logger.info(
                            "UPLOAD_COMPLETE job=%s channel=%s target=%s video=%s 100%%",
                            job.id,
                            target.channel_title,
                            target.id,
                            video_id,
                        )

                        if thumbnail is not None and credentials is not None:
                            try:
                                signed_url = await bucket.create_signed_url(
                                    thumbnail.storage_path
                                )
                                thumb_data = await asyncio.to_thread(
                                    youtube_api.fetch_bytes_sync,
                                    signed_url,
                                    max_bytes=settings.storage_image_max_bytes,
                                    timeout=settings.yt_upload_http_timeout_seconds,
                                )
                                await asyncio.to_thread(
                                    youtube_api.set_thumbnail_sync,
                                    credentials,
                                    video_id=video_id,
                                    data=thumb_data,
                                    content_type=thumbnail.content_type,
                                )
                                await crud.set_thumbnail_result(
                                    self._session,
                                    target.id,
                                    status=ThumbnailStatus.DONE,
                                    error=None,
                                )
                            except Exception as thumb_error:
                                await crud.set_thumbnail_result(
                                    self._session,
                                    target.id,
                                    status=ThumbnailStatus.FAILED,
                                    error=str(thumb_error),
                                )

                    await self._session.commit()

                    done_count = sum(
                        1
                        for item in job.targets
                        if item.status == UploadTargetStatus.DONE.value
                    )
                    failed_count = sum(
                        1
                        for item in job.targets
                        if item.status == UploadTargetStatus.FAILED.value
                    )
                    logger.info(
                        "UPLOAD_JOB_SUMMARY job=%s completed=%d/%d failed=%d active=%d",
                        job.id,
                        done_count,
                        len(job.targets),
                        failed_count,
                        len(active_tasks),
                    )

            await crud.refresh_job_status(self._session, job.id)
            await self._session.commit()
            fresh = await self._get_job(user_id, job.id)
            return _to_detail(fresh, await self._load_emails([fresh]), None)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

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

    async def get_upload(
        self, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> UploadJobDetail:
        job = await self._get_job(user_id, job_id)
        return _to_detail(job, await self._load_emails([job]), await self._asset_of(job))

    async def retry_upload(
        self, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> UploadJobDetail:
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

    async def cancel_upload(
        self, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> UploadJobDetail:
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
        return _to_detail(
            fresh,
            await self._load_emails([fresh]),
            await self._asset_of(fresh),
        )

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

    async def _get_job(
        self, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> YtUploadJob:
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
            raise ValidationError(
                "The thumbnail must be a JPG or PNG image from your library."
            )
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
                "Some of the selected YouTube channels were not found or disconnected."
            )
        needs_reconnect = [
            accounts[account_id].nickname
            for account_id in account_ids
            if accounts[account_id].needs_reconnect
        ]
        if needs_reconnect:
            raise ValidationError(
                "Reconnect these channels before uploading: "
                + ", ".join(needs_reconnect)
                + "."
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
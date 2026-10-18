# app/workers/yt_channel_management.py
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.core.database import SessionFactory
from app.core.exceptions import AccountDisabledError, AppException, NotFoundError
from app.crud import storage_bucket as storage_crud
from app.crud import yt_channel_management as crud
from app.integrations import youtube_api
from app.service.yt_management import YouTubeManagementService
from app.utils import bucket
from app.utils.datetime_utils import utc_now
from app.utils.yt_channel_management import (
    QUOTA_EXCEEDED_FAILURE,
    ThumbnailStatus,
    UploadCancelled,
    UploadFailure,
    UploadFailureError,
    UploadInterrupted,
    UploadTargetStatus,
    build_video_body,
    classify_error,
    quota_day,
    retry_delay_seconds,
    thumbnail_failure_message,
    watch_url,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Plan:
    target_id: uuid.UUID
    user_id: uuid.UUID
    account_id: uuid.UUID
    source_path: str
    size_bytes: int
    content_type: str
    body: dict
    notify_subscribers: bool
    wants_thumbnail: bool
    thumbnail_path: Optional[str]
    thumbnail_content_type: Optional[str]


@dataclass
class WorkerHandle:
    task: asyncio.Task
    stop_event: asyncio.Event


class YtUploadWorker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] = SessionFactory) -> None:
        self._session_factory = session_factory
        self._stop_event = asyncio.Event()

    async def run_forever(self, stop_event: Optional[asyncio.Event] = None) -> None:
        if stop_event is not None:
            self._stop_event = stop_event
        running: set[asyncio.Task] = set()
        last_sweep = 0.0
        logger.info("YouTube upload worker started")

        while not self._stop_event.is_set():
            running = {task for task in running if not task.done()}

            if time.monotonic() - last_sweep >= settings.yt_upload_stale_sweep_seconds:
                last_sweep = time.monotonic()
                await self._safely(self._sweep_stale())

            if len(running) < settings.yt_upload_concurrency:
                target_id = await self._safely(self._claim())
                if target_id is not None:
                    running.add(asyncio.create_task(self._run_target(target_id)))
                    continue

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=settings.yt_upload_worker_poll_seconds
                )
            except asyncio.TimeoutError:
                pass

        if running:
            logger.info("Waiting for %s running uploads to stop", len(running))
            _, pending = await asyncio.wait(
                running, timeout=settings.yt_upload_worker_shutdown_seconds
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("YouTube upload worker stopped")

    async def _safely(self, awaitable):
        try:
            return await awaitable
        except Exception:
            logger.exception("YouTube upload worker step failed")
            await asyncio.sleep(settings.yt_upload_worker_poll_seconds)
            return None

    async def _claim(self) -> Optional[uuid.UUID]:
        async with self._session_factory() as session:
            target = await crud.claim_next_target(session, now=utc_now())
            if target is None:
                return None
            await crud.refresh_job_status(session, target.job_id)
            await session.commit()
            return target.id

    async def _sweep_stale(self) -> None:
        now = utc_now()
        cutoff = now - timedelta(seconds=settings.yt_upload_stale_after_seconds)
        async with self._session_factory() as session:
            job_ids = await crud.release_stale_targets(
                session,
                cutoff=cutoff,
                max_attempts=settings.yt_upload_max_attempts,
                now=now,
            )
            for job_id in job_ids:
                await crud.refresh_job_status(session, job_id)
            await session.commit()
        if job_ids:
            logger.warning("Released interrupted uploads for %s jobs", len(job_ids))

    async def _run_target(self, target_id: uuid.UUID) -> None:
        failure: Optional[UploadFailure] = None
        try:
            await self._upload(target_id)
        except UploadCancelled:
            logger.info("Upload target %s was cancelled while uploading", target_id)
        except UploadInterrupted:
            await self._requeue(target_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = classify_error(exc)
            if failure.code == "unexpected_error":
                logger.exception("Upload target %s failed unexpectedly", target_id)
            else:
                logger.warning(
                    "Upload target %s failed (%s): %s", target_id, failure.code, exc
                )
        if failure is not None:
            await self._record_failure(target_id, failure)

    async def _upload(self, target_id: uuid.UUID) -> None:
        plan = await self._load_plan(target_id)
        if plan is None:
            raise UploadCancelled()
        credentials = await self._get_credentials(plan)
        await self._reserve_quota()
        source_url = await self._sign(plan.source_path)

        loop = asyncio.get_running_loop()
        on_progress = self._progress_callback(plan.target_id, loop)
        video_id = await asyncio.to_thread(
            youtube_api.upload_video_sync,
            credentials,
            source_url=source_url,
            size_bytes=plan.size_bytes,
            content_type=plan.content_type,
            body=plan.body,
            notify_subscribers=plan.notify_subscribers,
            on_progress=on_progress,
        )
        await self._record_success(plan, video_id)
        await self._apply_thumbnail(plan, credentials, video_id)

    async def _load_plan(self, target_id: uuid.UUID) -> Optional[_Plan]:
        async with self._session_factory() as session:
            target = await crud.get_target(session, target_id)
            if target is None or target.status != UploadTargetStatus.UPLOADING.value:
                return None
            job = await crud.get_job_by_id(session, target.job_id)
            if job is None:
                return None

            if target.account_id is None:
                raise UploadFailureError(
                    UploadFailure(
                        code="account_unavailable",
                        message="This YouTube channel was removed from your account.",
                    )
                )
            source = (
                await storage_crud.get_file(session, job.storage_file_id, job.user_id)
                if job.storage_file_id
                else None
            )
            if source is None:
                raise UploadFailureError(
                    UploadFailure(
                        code="source_missing",
                        message="The video is no longer in your library.",
                    )
                )
            thumbnail = (
                await storage_crud.get_file(session, job.thumbnail_file_id, job.user_id)
                if job.thumbnail_file_id
                else None
            )
            return _Plan(
                target_id=target.id,
                user_id=job.user_id,
                account_id=target.account_id,
                source_path=source.storage_path,
                size_bytes=source.size_bytes,
                content_type=source.content_type,
                body=build_video_body(
                    title=job.title,
                    description=job.description,
                    tags=list(job.tags or []),
                    category_id=job.category_id,
                    privacy_status=job.privacy_status,
                    publish_at=job.publish_at,
                    made_for_kids=job.made_for_kids,
                    default_language=job.default_language,
                ),
                notify_subscribers=job.notify_subscribers,
                wants_thumbnail=job.thumbnail_file_id is not None
                or job.thumbnail_file_name is not None,
                thumbnail_path=thumbnail.storage_path if thumbnail else None,
                thumbnail_content_type=thumbnail.content_type if thumbnail else None,
            )

    async def _get_credentials(self, plan: _Plan):
        async with self._session_factory() as session:
            try:
                return await YouTubeManagementService(session).get_fresh_credentials(
                    plan.user_id, plan.account_id
                )
            except (NotFoundError, AccountDisabledError) as exc:
                raise UploadFailureError(
                    UploadFailure(
                        code="account_unavailable",
                        message="This YouTube channel is disconnected. Connect it again to upload.",
                    )
                ) from exc
            except AppException as exc:
                raise UploadFailureError(
                    UploadFailure(
                        code="reconnect_required",
                        message=(
                            "This channel needs to be reconnected before it can upload. "
                            "Reconnect it and retry."
                        ),
                    )
                ) from exc

    async def _reserve_quota(self) -> None:
        async with self._session_factory() as session:
            allowed = await crud.try_reserve_quota(
                session,
                day=quota_day(utc_now()),
                limit=settings.yt_upload_daily_limit,
            )
            await session.commit()
        if not allowed:
            raise UploadFailureError(QUOTA_EXCEEDED_FAILURE)

    async def _sign(self, path: str) -> str:
        try:
            return await bucket.create_signed_url(
                path, expires_in=settings.yt_upload_source_url_ttl_seconds
            )
        except AppException as exc:
            raise UploadFailureError(
                UploadFailure(
                    code="storage_unavailable",
                    message="The video could not be read from storage right now.",
                    retryable=True,
                )
            ) from exc

    def _progress_callback(
        self, target_id: uuid.UUID, loop: asyncio.AbstractEventLoop
    ) -> Callable[[int], None]:
        state = {"last": 0.0}

        def on_progress(percent: int) -> None:
            if self._stop_event.is_set():
                raise UploadInterrupted()
            now = time.monotonic()
            if percent < 100 and now - state["last"] < settings.yt_upload_progress_interval_seconds:
                return
            state["last"] = now
            alive = asyncio.run_coroutine_threadsafe(
                self._heartbeat(target_id, percent), loop
            ).result(timeout=60)
            if not alive:
                raise UploadCancelled()

        return on_progress

    async def _heartbeat(self, target_id: uuid.UUID, percent: int) -> bool:
        async with self._session_factory() as session:
            alive = await crud.touch_target(
                session, target_id, progress=percent, now=utc_now()
            )
            await session.commit()
            return alive

    async def _record_success(self, plan: _Plan, video_id: str) -> None:
        now = utc_now()
        async with self._session_factory() as session:
            target = await crud.get_target(session, plan.target_id)
            if target is None:
                return
            await crud.mark_target_done(
                session,
                target,
                video_id=video_id,
                video_url=watch_url(video_id),
                now=now,
            )
            await crud.mark_account_used(session, plan.account_id, now=now)
            await crud.refresh_job_status(session, target.job_id)
            await session.commit()
        logger.info("Target %s uploaded as video %s", plan.target_id, video_id)

    async def _apply_thumbnail(self, plan: _Plan, credentials, video_id: str) -> None:
        if not plan.wants_thumbnail:
            return
        status = ThumbnailStatus.DONE
        error: Optional[str] = None
        if plan.thumbnail_path is None or plan.thumbnail_content_type is None:
            status = ThumbnailStatus.FAILED
            error = "The thumbnail image is no longer in your library."
        else:
            try:
                url = await self._sign(plan.thumbnail_path)
                data = await asyncio.to_thread(
                    youtube_api.fetch_bytes_sync,
                    url,
                    max_bytes=settings.storage_image_max_bytes,
                    timeout=settings.storage_request_timeout_seconds,
                )
                await asyncio.to_thread(
                    youtube_api.set_thumbnail_sync,
                    credentials,
                    video_id=video_id,
                    data=data,
                    content_type=plan.thumbnail_content_type,
                )
            except Exception as exc:
                logger.warning("Thumbnail for target %s failed: %s", plan.target_id, exc)
                status = ThumbnailStatus.FAILED
                error = thumbnail_failure_message(exc)
        async with self._session_factory() as session:
            await crud.set_thumbnail_result(
                session, plan.target_id, status=status, error=error
            )
            await session.commit()

    async def _record_failure(self, target_id: uuid.UUID, failure: UploadFailure) -> None:
        try:
            async with self._session_factory() as session:
                target = await crud.get_target(session, target_id)
                if target is None or target.status != UploadTargetStatus.UPLOADING.value:
                    return
                now = utc_now()
                if failure.retryable and target.attempts < settings.yt_upload_max_attempts:
                    await crud.mark_target_retrying(
                        session,
                        target,
                        code=failure.code,
                        message=failure.message,
                        next_attempt_at=now
                        + timedelta(seconds=retry_delay_seconds(target.attempts)),
                    )
                else:
                    await crud.mark_target_failed(
                        session,
                        target,
                        code=failure.code,
                        message=failure.message,
                        now=now,
                    )
                if failure.needs_reconnect and target.account_id is not None:
                    await crud.flag_account_needs_reconnect(session, target.account_id)
                affected = {target.job_id}
                if failure.stop_all:
                    affected |= await crud.fail_waiting_targets(
                        session, code=failure.code, message=failure.message, now=now
                    )
                for job_id in affected:
                    await crud.refresh_job_status(session, job_id)
                await session.commit()
        except Exception:
            logger.exception("Could not record the failure of target %s", target_id)

    async def _requeue(self, target_id: uuid.UUID) -> None:
        try:
            async with self._session_factory() as session:
                target = await crud.get_target(session, target_id)
                if target is None or target.status != UploadTargetStatus.UPLOADING.value:
                    return
                await crud.requeue_target(session, target)
                await crud.refresh_job_status(session, target.job_id)
                await session.commit()
        except Exception:
            logger.exception("Could not requeue target %s", target_id)


def start_worker() -> WorkerHandle:
    stop_event = asyncio.Event()
    worker = YtUploadWorker()
    task = asyncio.create_task(worker.run_forever(stop_event))
    return WorkerHandle(task=task, stop_event=stop_event)


async def stop_worker(handle: WorkerHandle) -> None:
    handle.stop_event.set()
    try:
        await asyncio.wait_for(
            handle.task, timeout=settings.yt_upload_worker_shutdown_seconds + 10
        )
    except asyncio.TimeoutError:
        handle.task.cancel()
        await asyncio.gather(handle.task, return_exceptions=True)


async def main() -> None:
    from app.core.database import dispose_engine
    from app.core.logging import setup_logging

    setup_logging()
    stop_event = asyncio.Event()
    try:
        await YtUploadWorker().run_forever(stop_event)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
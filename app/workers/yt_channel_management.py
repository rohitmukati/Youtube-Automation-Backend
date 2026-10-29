# app/workers/yt_channel_management.py
import asyncio
import functools
import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.core.database import SessionFactory
from app.core.exceptions import (
    AccountDisabledError,
    AppException,
    GoogleCredentialsMissingError,
    NotFoundError,
)
from app.crud import storage_bucket as storage_crud
from app.crud import yt_channel_management as crud
from app.crud import yt_media_upload as media_crud
from app.integrations import youtube_api
from app.service.yt_management import YouTubeManagementService
from app.service.yt_media_upload import YtMediaUploadService, run_reaper_once
from app.utils import bucket
from app.utils.datetime_utils import utc_now
from app.utils.token_encryption import decrypt_token, encrypt_token
from app.utils.yt_channel_management import (
    QUOTA_EXCEEDED_FAILURE,
    ThumbnailStatus,
    UploadCancelled,
    UploadFailure,
    UploadFailureError,
    UploadInterrupted,
    UploadJobStatus,
    UploadTargetStatus,
    build_video_body,
    classify_error,
    next_quota_reset,
    quota_day,
    retry_delay_seconds,
    thumbnail_failure_message,
    watch_url,
)
from app.utils.yt_media_upload import MediaAssetStatus

logger = logging.getLogger(__name__)

_RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
_TRANSACTION_ATTEMPTS = 4
_QUOTA_RESUME_MARGIN_SECONDS = 60
_QUOTA_WAIT_MESSAGE = (
    "The daily YouTube upload limit was reached. "
    "This channel continues automatically after the limit resets."
)


def _sqlstate(exc: DBAPIError) -> str:
    original = getattr(exc, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return ""


@dataclass(frozen=True)
class _Plan:
    target_id: uuid.UUID
    lease_token: str
    job_id: uuid.UUID
    user_id: uuid.UUID
    account_id: uuid.UUID
    source_key: str
    size_bytes: int
    content_type: str
    body: dict
    notify_subscribers: bool
    wants_thumbnail: bool
    thumbnail_path: Optional[str]
    thumbnail_content_type: Optional[str]
    encrypted_session: Optional[str]


@dataclass
class WorkerHandle:
    task: asyncio.Task
    stop_event: asyncio.Event


class YtUploadWorker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] = SessionFactory) -> None:
        self._session_factory = session_factory
        self._stop_event = asyncio.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=settings.yt_upload_concurrency + 4,
            thread_name_prefix="yt-upload",
        )

    async def run_forever(self, stop_event: Optional[asyncio.Event] = None) -> None:
        if stop_event is not None:
            self._stop_event = stop_event
        running: set[asyncio.Task] = set()
        last_sweep = 0.0
        last_reap = 0.0
        logger.info("YouTube upload worker started")

        while not self._stop_event.is_set():
            running = {task for task in running if not task.done()}

            if time.monotonic() - last_sweep >= settings.yt_upload_stale_sweep_seconds:
                last_sweep = time.monotonic()
                await self._safely(self._sweep_stale())

            if time.monotonic() - last_reap >= settings.media_reaper_interval_seconds:
                last_reap = time.monotonic()
                await self._safely(run_reaper_once(self._session_factory))

            if len(running) < settings.yt_upload_concurrency:
                claimed = await self._safely(self._claim())
                if claimed is not None:
                    running.add(asyncio.create_task(self._run_target(*claimed)))
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
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("YouTube upload worker stopped")

    async def _safely(self, awaitable):
        try:
            return await awaitable
        except Exception:
            logger.exception("YouTube upload worker step failed")
            await asyncio.sleep(settings.yt_upload_worker_poll_seconds)
            return None

    async def _run_transaction(self, work):
        for attempt in range(1, _TRANSACTION_ATTEMPTS + 1):
            try:
                async with self._session_factory() as session:
                    result = await work(session)
                    await session.commit()
                    return result
            except DBAPIError as exc:
                state = _sqlstate(exc)
                if attempt == _TRANSACTION_ATTEMPTS or state not in _RETRYABLE_SQLSTATES:
                    raise
                logger.warning("Database conflict (%s), trying again (%s)", state, attempt)
                await asyncio.sleep(0.05 * attempt + random.random() * 0.05)

    async def _refresh_jobs(self, job_ids) -> dict:
        if not job_ids:
            return {}

        async def work(session):
            return {
                job_id: await crud.refresh_job_status(session, job_id)
                for job_id in sorted(job_ids)
            }

        return await self._run_transaction(work)

    async def _claim(self) -> Optional[tuple[uuid.UUID, str]]:
        async def work(session):
            target = await crud.claim_next_target(session, now=utc_now())
            if target is None:
                return None
            return target.id, str(target.lease_token), target.job_id

        claimed = await self._run_transaction(work)
        if claimed is None:
            return None
        target_id, lease_token, job_id = claimed
        await self._refresh_jobs({job_id})
        return target_id, lease_token

    async def _sweep_stale(self) -> None:
        now = utc_now()
        cutoff = now - timedelta(seconds=settings.yt_upload_stale_after_seconds)

        async def work(session):
            return await crud.release_stale_targets(
                session,
                cutoff=cutoff,
                max_attempts=settings.yt_upload_max_attempts,
                now=now,
            )

        job_ids = await self._run_transaction(work)
        await self._refresh_jobs(job_ids)
        if job_ids:
            logger.warning("Released interrupted uploads for %s jobs", len(job_ids))

    async def _run_target(self, target_id: uuid.UUID, lease_token: str) -> None:
        failure: Optional[UploadFailure] = None
        try:
            await self._upload(target_id, lease_token)
        except UploadCancelled:
            logger.info("Upload target %s was cancelled while uploading", target_id)
        except UploadInterrupted:
            await self._requeue(target_id, lease_token)
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
            await self._record_failure(target_id, lease_token, failure)

    async def _in_thread(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    async def _upload(self, target_id: uuid.UUID, lease_token: str) -> None:
        plan = await self._load_plan(target_id, lease_token)
        if plan is None:
            raise UploadCancelled()
        credentials = await self._get_credentials(plan)

        cancel = threading.Event()
        heartbeat = asyncio.create_task(self._heartbeat(plan, cancel))
        try:
            # _transfer owns the RangeReader lifecycle and refreshes the signed URL
            # before each new session so an expired URL never causes a silent 404.
            video_id = await self._transfer(plan, credentials, cancel)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        await self._record_success(plan, video_id)
        await self._apply_thumbnail(plan, credentials, video_id)
        await self._cleanup_if_completed(plan.job_id)

    async def _transfer(
        self,
        plan: _Plan,
        credentials,
        cancel: threading.Event,
    ) -> str:
        session_uri = self._read_session(plan)
        loop = asyncio.get_running_loop()
        check_stop = self._stop_checker(cancel)
        on_progress = self._progress_callback(plan, loop, cancel)

        # Two attempts: one for a normal run, one after a YouTube session expiry.
        # A fresh signed URL is generated before each attempt so that an expired
        # presigned URL never causes a 404 when the upload is resumed or retried.
        for attempt in range(2):
            source_url = await self._sign(plan.source_key)
            reader = youtube_api.RangeReader(source_url, plan.size_bytes)
            try:
                if session_uri is None:
                    session_uri = await self._start_session(plan, credentials, reader)
                return await self._in_thread(
                    youtube_api.upload_resumable,
                    credentials,
                    session_uri=session_uri,
                    reader=reader,
                    size_bytes=plan.size_bytes,
                    on_progress=on_progress,
                    check_stop=check_stop,
                )
            except youtube_api.SessionExpiredError:
                logger.info(
                    "Upload session of target %s expired on attempt %d, starting a new one",
                    plan.target_id,
                    attempt + 1,
                )
                await self._save_session(plan, None)
                session_uri = None
            finally:
                reader.close()

        raise UploadFailureError(
            UploadFailure(
                code="youtube_unavailable",
                message="YouTube had a temporary problem while uploading.",
                retryable=True,
            )
        )

    def _read_session(self, plan: _Plan) -> Optional[str]:
        if not plan.encrypted_session:
            return None
        try:
            return decrypt_token(plan.encrypted_session)
        except RuntimeError:
            logger.warning("Saved upload session of target %s could not be read", plan.target_id)
            return None

    async def _start_session(self, plan: _Plan, credentials, reader) -> str:
        await self._in_thread(reader.read, 0, 1)
        await self._reserve_quota(plan.user_id)
        day = quota_day(utc_now())
        try:
            uri = await self._in_thread(
                youtube_api.initiate_session,
                credentials,
                body=plan.body,
                size_bytes=plan.size_bytes,
                content_type=plan.content_type,
                notify_subscribers=plan.notify_subscribers,
            )
        except Exception as exc:
            failure = classify_error(exc)
            if not (failure.code == "network_error" and failure.reached_server):
                await self._release_quota(plan.user_id, day)
            raise
        if not await self._save_session(plan, encrypt_token(uri)):
            raise UploadCancelled()
        return uri

    async def _load_plan(self, target_id: uuid.UUID, lease_token: str) -> Optional[_Plan]:
        async with self._session_factory() as session:
            target = await crud.get_target(session, target_id)
            if (
                target is None
                or target.status != UploadTargetStatus.UPLOADING.value
                or target.lease_token != lease_token
            ):
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
            asset = (
                await media_crud.get_asset(session, job.media_asset_id)
                if job.media_asset_id
                else None
            )
            if asset is None or asset.status != MediaAssetStatus.READY.value:
                actual_status = asset.status if asset is not None else "not found"
                logger.warning(
                    "Target %s: media asset %s is not ready (status=%s); "
                    "was READY when claimed but changed before _load_plan ran",
                    target_id,
                    job.media_asset_id,
                    actual_status,
                )
                raise UploadFailureError(
                    UploadFailure(
                        code="source_missing",
                        message=(
                            "The video file is no longer available. "
                            "Start a new upload to publish it."
                        ),
                    )
                )
            thumbnail = (
                await storage_crud.get_file(session, job.thumbnail_file_id, job.user_id)
                if job.thumbnail_file_id
                else None
            )
            return _Plan(
                target_id=target.id,
                lease_token=lease_token,
                job_id=job.id,
                user_id=job.user_id,
                account_id=target.account_id,
                source_key=asset.object_key,
                size_bytes=asset.size_bytes,
                content_type=asset.content_type,
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
                encrypted_session=target.resumable_uri,
            )

    async def _get_credentials(self, plan: _Plan):
        async with self._session_factory() as session:
            try:
                return await YouTubeManagementService(session).get_fresh_credentials(
                    plan.user_id, plan.account_id
                )
            except GoogleCredentialsMissingError as exc:
                raise UploadFailureError(
                    UploadFailure(code=exc.code, message=exc.message)
                ) from exc
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
                        needs_reconnect=True,
                    )
                ) from exc

    async def _reserve_quota(self, user_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            allowed = await crud.try_reserve_quota(
                session,
                user_id=user_id,
                day=quota_day(utc_now()),
                limit=settings.yt_upload_daily_limit,
            )
            await session.commit()
        if not allowed:
            raise UploadFailureError(QUOTA_EXCEEDED_FAILURE)

    async def _release_quota(self, user_id: uuid.UUID, day) -> None:
        try:
            async with self._session_factory() as session:
                await crud.release_quota(session, user_id=user_id, day=day)
                await session.commit()
        except Exception:
            logger.exception("Could not give back the upload credit of user %s", user_id)

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

    def _stop_checker(self, cancel: threading.Event) -> Callable[[], None]:
        def check_stop() -> None:
            if cancel.is_set():
                raise UploadCancelled()
            if self._stop_event.is_set():
                raise UploadInterrupted()

        return check_stop

    def _progress_callback(
        self, plan: _Plan, loop: asyncio.AbstractEventLoop, cancel: threading.Event
    ) -> Callable[[int], None]:
        state = {"last": 0.0}

        def on_progress(confirmed: int) -> None:
            now = time.monotonic()
            finished = confirmed >= plan.size_bytes
            if not finished and now - state["last"] < settings.yt_upload_progress_interval_seconds:
                return
            state["last"] = now
            percent = min(int(confirmed * 100 / plan.size_bytes), 99)
            alive = asyncio.run_coroutine_threadsafe(
                self._progress(plan, percent, confirmed), loop
            ).result(timeout=60)
            if not alive:
                cancel.set()

        return on_progress

    async def _progress(self, plan: _Plan, percent: int, confirmed: int) -> bool:
        async with self._session_factory() as session:
            alive = await crud.touch_target(
                session,
                plan.target_id,
                plan.lease_token,
                now=utc_now(),
                progress=percent,
                bytes_confirmed=confirmed,
            )
            await session.commit()
            return alive

    async def _heartbeat(self, plan: _Plan, cancel: threading.Event) -> None:
        while True:
            await asyncio.sleep(settings.yt_upload_heartbeat_seconds)
            try:
                async with self._session_factory() as session:
                    alive = await crud.touch_target(
                        session, plan.target_id, plan.lease_token, now=utc_now()
                    )
                    await session.commit()
            except Exception:
                logger.exception("Heartbeat of target %s failed", plan.target_id)
                continue
            if not alive:
                cancel.set()
                return

    async def _save_session(self, plan: _Plan, encrypted_uri: Optional[str]) -> bool:
        async with self._session_factory() as session:
            saved = await crud.save_session(
                session, plan.target_id, plan.lease_token, encrypted_uri=encrypted_uri
            )
            await session.commit()
            return saved

    async def _record_success(self, plan: _Plan, video_id: str) -> None:
        now = utc_now()

        async def work(session):
            recorded = await crud.mark_target_done(
                session,
                plan.target_id,
                plan.lease_token,
                video_id=video_id,
                video_url=watch_url(video_id),
                now=now,
            )
            if recorded:
                await crud.mark_account_used(session, plan.account_id, now=now)
            return recorded

        if not await self._run_transaction(work):
            logger.warning(
                "Target %s finished as video %s but its lease was lost, so the result "
                "was not saved",
                plan.target_id,
                video_id,
            )
            return
        await self._refresh_jobs({plan.job_id})
        logger.info("Target %s uploaded as video %s", plan.target_id, video_id)

    async def _cleanup_if_completed(self, job_id: uuid.UUID) -> None:
        try:
            async with self._session_factory() as session:
                job = await crud.get_job_by_id(session, job_id)
                if (
                    job is None
                    or job.status != UploadJobStatus.COMPLETED.value
                    or job.media_asset_id is None
                ):
                    return
                await YtMediaUploadService(session).discard(job.media_asset_id)
                await session.commit()
        except Exception:
            logger.warning("Could not remove the staged file of job %s, cleanup will retry", job_id)

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
                data = await self._in_thread(
                    youtube_api.fetch_bytes_sync,
                    url,
                    max_bytes=settings.storage_image_max_bytes,
                    timeout=settings.storage_request_timeout_seconds,
                )
                await self._in_thread(
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

    async def _record_failure(
        self, target_id: uuid.UUID, lease_token: str, failure: UploadFailure
    ) -> None:
        async def work(session):
            target = await crud.get_target(session, target_id)
            if (
                target is None
                or target.status != UploadTargetStatus.UPLOADING.value
                or target.lease_token != lease_token
            ):
                return None
            now = utc_now()
            affected = {target.job_id}
            if failure.stop_all:
                owner = await crud.get_job_by_id(session, target.job_id)
                await crud.requeue_target(session, target)
                if owner is not None:
                    resume_at = next_quota_reset(now) + timedelta(
                        seconds=_QUOTA_RESUME_MARGIN_SECONDS
                    )
                    affected |= await crud.defer_waiting_targets(
                        session,
                        user_id=owner.user_id,
                        code=failure.code,
                        message=_QUOTA_WAIT_MESSAGE,
                        fail_message=failure.message,
                        resume_at=resume_at,
                        now=now,
                    )
            elif failure.retryable and target.attempts < settings.yt_upload_max_attempts:
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
            return affected

        try:
            affected = await self._run_transaction(work)
            if affected:
                await self._refresh_jobs(affected)
        except Exception:
            logger.exception("Could not record the failure of target %s", target_id)

    async def _requeue(self, target_id: uuid.UUID, lease_token: str) -> None:
        async def work(session):
            target = await crud.get_target(session, target_id)
            if (
                target is None
                or target.status != UploadTargetStatus.UPLOADING.value
                or target.lease_token != lease_token
            ):
                return None
            await crud.requeue_target(session, target)
            return target.job_id

        try:
            job_id = await self._run_transaction(work)
            if job_id is not None:
                await self._refresh_jobs({job_id})
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
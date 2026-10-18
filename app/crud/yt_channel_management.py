# app/crud/yt_channel_management.py
import uuid
from datetime import date, datetime
from typing import Optional, Sequence

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_bucket import StorageFile
from app.models.yt_channel_management import YtUploadJob, YtUploadQuotaDay, YtUploadTarget
from app.models.yt_management import YouTubeAccount
from app.utils.datetime_utils import utc_now
from app.utils.yt_channel_management import (
    ACTIVE_TARGET_STATUSES,
    WAITING_TARGET_STATUSES,
    ThumbnailStatus,
    UploadJobStatus,
    UploadTargetStatus,
    compute_job_status,
    shorten,
)


def _initial_thumbnail_status(has_thumbnail: bool) -> str:
    return ThumbnailStatus.PENDING.value if has_thumbnail else ThumbnailStatus.NONE.value


async def create_job(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    source_file: StorageFile,
    thumbnail_file: Optional[StorageFile],
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    privacy_status: str,
    publish_at: Optional[datetime],
    made_for_kids: bool,
    default_language: Optional[str],
    notify_subscribers: bool,
    accounts: Sequence[YouTubeAccount],
) -> YtUploadJob:
    job = YtUploadJob(
        id=uuid.uuid4(),
        user_id=user_id,
        storage_file_id=source_file.id,
        source_file_name=source_file.original_name,
        source_content_type=source_file.content_type,
        source_size_bytes=source_file.size_bytes,
        thumbnail_file_id=thumbnail_file.id if thumbnail_file else None,
        thumbnail_file_name=thumbnail_file.original_name if thumbnail_file else None,
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        privacy_status=privacy_status,
        publish_at=publish_at,
        made_for_kids=made_for_kids,
        default_language=default_language,
        notify_subscribers=notify_subscribers,
        status=UploadJobStatus.QUEUED.value,
        targets=[
            YtUploadTarget(
                id=uuid.uuid4(),
                account_id=account.id,
                channel_id=account.channel_id,
                channel_title=account.channel_title,
                account_nickname=account.nickname,
                status=UploadTargetStatus.PENDING.value,
                progress_percent=0,
                attempts=0,
                thumbnail_status=_initial_thumbnail_status(thumbnail_file is not None),
            )
            for account in accounts
        ],
    )
    session.add(job)
    await session.flush()
    return job


async def get_job(
    session: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[YtUploadJob]:
    result = await session.execute(
        select(YtUploadJob)
        .where(YtUploadJob.id == job_id, YtUploadJob.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_job_by_id(session: AsyncSession, job_id: uuid.UUID) -> Optional[YtUploadJob]:
    result = await session.execute(
        select(YtUploadJob)
        .where(YtUploadJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _apply_job_filters(
    statement,
    *,
    user_id: uuid.UUID,
    status: Optional[UploadJobStatus],
    account_id: Optional[uuid.UUID],
    search: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
):
    statement = statement.where(YtUploadJob.user_id == user_id)
    if status is not None:
        statement = statement.where(YtUploadJob.status == status.value)
    if account_id is not None:
        statement = statement.where(
            YtUploadJob.id.in_(
                select(YtUploadTarget.job_id).where(YtUploadTarget.account_id == account_id)
            )
        )
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(YtUploadJob.title.ilike(f"%{escaped}%", escape="\\"))
    if date_from is not None:
        statement = statement.where(YtUploadJob.created_at >= date_from)
    if date_to is not None:
        statement = statement.where(YtUploadJob.created_at <= date_to)
    return statement


async def list_jobs(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    status: Optional[UploadJobStatus],
    account_id: Optional[uuid.UUID],
    search: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    limit: int,
    offset: int,
) -> tuple[list[YtUploadJob], int]:
    filters = {
        "user_id": user_id,
        "status": status,
        "account_id": account_id,
        "search": search,
        "date_from": date_from,
        "date_to": date_to,
    }
    total = await session.scalar(
        _apply_job_filters(select(func.count()).select_from(YtUploadJob), **filters)
    )
    result = await session.execute(
        _apply_job_filters(select(YtUploadJob), **filters)
        .order_by(YtUploadJob.created_at.desc(), YtUploadJob.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def get_accounts_by_ids(
    session: AsyncSession, account_ids: Sequence[uuid.UUID], user_id: uuid.UUID
) -> dict[uuid.UUID, YouTubeAccount]:
    if not account_ids:
        return {}
    result = await session.execute(
        select(YouTubeAccount).where(
            YouTubeAccount.id.in_(list(account_ids)),
            YouTubeAccount.user_id == user_id,
        )
    )
    return {account.id: account for account in result.scalars().all()}


async def get_account_emails(
    session: AsyncSession, account_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Optional[str]]:
    if not account_ids:
        return {}
    rows = await session.execute(
        select(YouTubeAccount.id, YouTubeAccount.google_email).where(
            YouTubeAccount.id.in_(list(account_ids))
        )
    )
    return {row[0]: row[1] for row in rows.all()}


async def get_target(session: AsyncSession, target_id: uuid.UUID) -> Optional[YtUploadTarget]:
    result = await session.execute(
        select(YtUploadTarget)
        .where(YtUploadTarget.id == target_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def claim_next_target(
    session: AsyncSession, *, now: datetime
) -> Optional[YtUploadTarget]:
    statement = (
        select(YtUploadTarget)
        .where(
            or_(
                YtUploadTarget.status == UploadTargetStatus.PENDING.value,
                and_(
                    YtUploadTarget.status == UploadTargetStatus.RETRYING.value,
                    YtUploadTarget.next_attempt_at <= now,
                ),
            )
        )
        .order_by(YtUploadTarget.created_at, YtUploadTarget.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    target = (await session.execute(statement)).scalar_one_or_none()
    if target is None:
        return None
    target.status = UploadTargetStatus.UPLOADING.value
    target.attempts = target.attempts + 1
    target.progress_percent = 0
    target.locked_at = now
    target.next_attempt_at = None
    target.finished_at = None
    if target.started_at is None:
        target.started_at = now
    await session.flush()
    return target


async def touch_target(
    session: AsyncSession, target_id: uuid.UUID, *, progress: int, now: datetime
) -> bool:
    result = await session.execute(
        update(YtUploadTarget)
        .where(
            YtUploadTarget.id == target_id,
            YtUploadTarget.status == UploadTargetStatus.UPLOADING.value,
        )
        .values(progress_percent=progress, locked_at=now)
    )
    return (result.rowcount or 0) > 0


async def release_stale_targets(
    session: AsyncSession, *, cutoff: datetime, max_attempts: int, now: datetime
) -> set[uuid.UUID]:
    result = await session.execute(
        select(YtUploadTarget)
        .where(
            YtUploadTarget.status == UploadTargetStatus.UPLOADING.value,
            or_(YtUploadTarget.locked_at.is_(None), YtUploadTarget.locked_at < cutoff),
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    job_ids: set[uuid.UUID] = set()
    for target in result.scalars().all():
        job_ids.add(target.job_id)
        target.locked_at = None
        if target.attempts >= max_attempts:
            target.status = UploadTargetStatus.FAILED.value
            target.error_code = "interrupted"
            target.error_message = "The upload was interrupted and could not be completed."
            target.finished_at = now
        else:
            target.status = UploadTargetStatus.PENDING.value
    await session.flush()
    return job_ids


async def mark_target_done(
    session: AsyncSession,
    target: YtUploadTarget,
    *,
    video_id: str,
    video_url: str,
    now: datetime,
) -> None:
    target.status = UploadTargetStatus.DONE.value
    target.progress_percent = 100
    target.youtube_video_id = video_id
    target.youtube_url = video_url
    target.error_code = None
    target.error_message = None
    target.locked_at = None
    target.next_attempt_at = None
    target.finished_at = now
    await session.flush()


async def mark_target_retrying(
    session: AsyncSession,
    target: YtUploadTarget,
    *,
    code: str,
    message: str,
    next_attempt_at: datetime,
) -> None:
    target.status = UploadTargetStatus.RETRYING.value
    target.error_code = code
    target.error_message = shorten(message)
    target.next_attempt_at = next_attempt_at
    target.locked_at = None
    await session.flush()


async def mark_target_failed(
    session: AsyncSession,
    target: YtUploadTarget,
    *,
    code: str,
    message: str,
    now: datetime,
) -> None:
    target.status = UploadTargetStatus.FAILED.value
    target.error_code = code
    target.error_message = shorten(message)
    target.next_attempt_at = None
    target.locked_at = None
    target.finished_at = now
    await session.flush()


async def requeue_target(session: AsyncSession, target: YtUploadTarget) -> None:
    target.status = UploadTargetStatus.PENDING.value
    target.attempts = max(target.attempts - 1, 0)
    target.locked_at = None
    target.progress_percent = 0
    await session.flush()


async def set_thumbnail_result(
    session: AsyncSession,
    target_id: uuid.UUID,
    *,
    status: ThumbnailStatus,
    error: Optional[str],
) -> None:
    await session.execute(
        update(YtUploadTarget)
        .where(YtUploadTarget.id == target_id)
        .values(thumbnail_status=status.value, thumbnail_error=shorten(error))
    )


async def fail_waiting_targets(
    session: AsyncSession, *, code: str, message: str, now: datetime
) -> set[uuid.UUID]:
    rows = (
        await session.execute(
            select(YtUploadTarget.id, YtUploadTarget.job_id).where(
                YtUploadTarget.status.in_(WAITING_TARGET_STATUSES)
            )
        )
    ).all()
    if not rows:
        return set()
    await session.execute(
        update(YtUploadTarget)
        .where(YtUploadTarget.id.in_([row[0] for row in rows]))
        .values(
            status=UploadTargetStatus.FAILED.value,
            error_code=code,
            error_message=shorten(message),
            next_attempt_at=None,
            locked_at=None,
            finished_at=now,
        )
    )
    return {row[1] for row in rows}


async def cancel_active_targets(
    session: AsyncSession, job_id: uuid.UUID, *, now: datetime
) -> int:
    result = await session.execute(
        update(YtUploadTarget)
        .where(
            YtUploadTarget.job_id == job_id,
            YtUploadTarget.status.in_(ACTIVE_TARGET_STATUSES),
        )
        .values(
            status=UploadTargetStatus.CANCELLED.value,
            next_attempt_at=None,
            locked_at=None,
            finished_at=now,
        )
    )
    return result.rowcount or 0


async def reset_targets(
    session: AsyncSession, target_ids: Sequence[uuid.UUID], *, has_thumbnail: bool
) -> None:
    if not target_ids:
        return
    await session.execute(
        update(YtUploadTarget)
        .where(YtUploadTarget.id.in_(list(target_ids)))
        .values(
            status=UploadTargetStatus.PENDING.value,
            attempts=0,
            progress_percent=0,
            next_attempt_at=None,
            locked_at=None,
            started_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
            thumbnail_status=_initial_thumbnail_status(has_thumbnail),
            thumbnail_error=None,
        )
    )


async def refresh_job_status(session: AsyncSession, job_id: uuid.UUID) -> UploadJobStatus:
    statuses = (
        (
            await session.execute(
                select(YtUploadTarget.status).where(YtUploadTarget.job_id == job_id)
            )
        )
        .scalars()
        .all()
    )
    new_status = compute_job_status(statuses)
    await session.execute(
        update(YtUploadJob)
        .where(YtUploadJob.id == job_id)
        .values(status=new_status.value, updated_at=utc_now())
    )
    return new_status


async def count_waiting_targets(session: AsyncSession) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(YtUploadTarget)
        .where(YtUploadTarget.status.in_(WAITING_TARGET_STATUSES))
    )
    return int(total or 0)


async def get_quota_calls(session: AsyncSession, day: date) -> int:
    calls = await session.scalar(select(YtUploadQuotaDay.calls).where(YtUploadQuotaDay.day == day))
    return int(calls or 0)


async def try_reserve_quota(session: AsyncSession, *, day: date, limit: int) -> bool:
    if limit <= 0:
        return False
    for _ in range(2):
        result = await session.execute(
            update(YtUploadQuotaDay)
            .where(YtUploadQuotaDay.day == day, YtUploadQuotaDay.calls < limit)
            .values(calls=YtUploadQuotaDay.calls + 1, updated_at=utc_now())
        )
        if (result.rowcount or 0) > 0:
            return True
        existing = await session.scalar(
            select(YtUploadQuotaDay.day).where(YtUploadQuotaDay.day == day)
        )
        if existing is not None:
            return False
        session.add(YtUploadQuotaDay(day=day, calls=1, updated_at=utc_now()))
        try:
            await session.flush()
            return True
        except IntegrityError:
            await session.rollback()
    return False


async def flag_account_needs_reconnect(session: AsyncSession, account_id: uuid.UUID) -> None:
    await session.execute(
        update(YouTubeAccount)
        .where(YouTubeAccount.id == account_id)
        .values(needs_reconnect=True)
    )


async def mark_account_used(
    session: AsyncSession, account_id: uuid.UUID, *, now: datetime
) -> None:
    await session.execute(
        update(YouTubeAccount).where(YouTubeAccount.id == account_id).values(last_used_at=now)
    )
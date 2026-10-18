# app/routes/yt_channel_management.py
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Query, status

from app.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.schemas.yt_channel_management import (
    CreateUploadRequest,
    UploadJobDetail,
    UploadJobListResponse,
    UploadQuotaResponse,
)
from app.service.yt_channel_management import YtChannelManagementService
from app.utils.yt_channel_management import UploadJobStatus

router = APIRouter(prefix="/yt-channels", tags=["YouTube Channel Management"])


@router.post(
    "/uploads",
    response_model=UploadJobDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Upload one video to many YouTube channels",
    description=(
        "Pick a video from your library (upload it to the library first if it is on your "
        "computer), then choose the connected channels. The same title, description, tags "
        "and settings are used for every channel. The upload runs in the background; "
        "follow it with the job detail endpoint."
    ),
)
async def create_upload(
    payload: CreateUploadRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> UploadJobDetail:
    return await YtChannelManagementService(session).create_upload(current_user.id, payload)


@router.get(
    "/uploads/quota",
    response_model=UploadQuotaResponse,
    summary="How many uploads are left today",
)
async def get_quota(
    current_user: CurrentUser,
    session: DbSession,
) -> UploadQuotaResponse:
    return await YtChannelManagementService(session).get_quota(current_user.id)


@router.get(
    "/uploads",
    response_model=UploadJobListResponse,
    summary="Upload history",
    description=(
        "Every upload you have started, newest first. Filter by status, channel, "
        "title and date range."
    ),
)
async def list_uploads(
    current_user: CurrentUser,
    session: DbSession,
    job_status: Annotated[Optional[UploadJobStatus], Query(alias="status")] = None,
    account_id: Annotated[Optional[uuid.UUID], Query()] = None,
    search: Annotated[Optional[str], Query(max_length=100)] = None,
    date_from: Annotated[Optional[datetime], Query()] = None,
    date_to: Annotated[Optional[datetime], Query()] = None,
    limit: Annotated[
        int, Query(ge=1, le=settings.yt_list_max_limit)
    ] = settings.yt_list_default_limit,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UploadJobListResponse:
    return await YtChannelManagementService(session).list_uploads(
        current_user.id,
        status=job_status,
        account_id=account_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/uploads/{job_id}",
    response_model=UploadJobDetail,
    summary="Upload detail with the status of every channel",
)
async def get_upload(
    job_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> UploadJobDetail:
    return await YtChannelManagementService(session).get_upload(current_user.id, job_id)


@router.post(
    "/uploads/{job_id}/retry",
    response_model=UploadJobDetail,
    summary="Retry the channels that failed",
)
async def retry_upload(
    job_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> UploadJobDetail:
    return await YtChannelManagementService(session).retry_upload(current_user.id, job_id)


@router.post(
    "/uploads/{job_id}/cancel",
    response_model=UploadJobDetail,
    summary="Cancel the channels that have not finished",
)
async def cancel_upload(
    job_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> UploadJobDetail:
    return await YtChannelManagementService(session).cancel_upload(current_user.id, job_id)
# app/routes/yt_channel_management.py
import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Query, status

from app.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.schemas.yt_channel_management import (
    CreateUploadRequest,
    StartUploadResponse,
    UploadJobDetail,
    UploadJobListResponse,
    UploadQuotaResponse,
)
from app.schemas.yt_media_upload import MediaStateResponse
from app.service.yt_channel_management import YtChannelManagementService
from app.utils.yt_channel_management import UploadJobStatus

router = APIRouter(prefix="/yt-channels", tags=["YouTube Channel Management"])


@router.post(
    "/uploads",
    response_model=StartUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start publishing one video to many YouTube channels",
    description=(
        "Send the video's name, size and type together with the title, description, "
        "tags and the channels. Nothing is uploaded until you call this. The response "
        "contains presigned URLs: the browser sends the video straight to storage in "
        "parts, then calls the complete endpoint. Publishing to every channel starts "
        "automatically once the file is complete. Send the same client_request_id on a "
        "repeated request to get the same upload back instead of creating a second one."
    ),
)
async def create_upload(
    payload: CreateUploadRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> StartUploadResponse:
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


@router.get(
    "/uploads/{job_id}/media",
    response_model=MediaStateResponse,
    summary="Upload state of the video file, with fresh URLs for the missing parts",
    description=(
        "Use this to resume a browser upload after a refresh or a lost connection. "
        "It lists the parts already stored and returns new presigned URLs for the rest."
    ),
)
async def get_media_state(
    job_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> MediaStateResponse:
    return await YtChannelManagementService(session).get_media_state(current_user.id, job_id)


@router.post(
    "/uploads/{job_id}/media/complete",
    response_model=UploadJobDetail,
    summary="Tell the server that every part has been uploaded",
    description=(
        "The server checks every part itself, joins them into the final file and "
        "starts publishing to the selected channels. Calling it again is safe."
    ),
)
async def complete_media(
    job_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> UploadJobDetail:
    return await YtChannelManagementService(session).complete_media(current_user.id, job_id)


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
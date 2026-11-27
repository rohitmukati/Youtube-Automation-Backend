# app/routes/video_downloader.py
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.core.dependencies import CurrentUser
from app.schemas.video_downloader import (
    PrepareDownloadRequest,
    PrepareDownloadResponse,
    VideoInfoRequest,
    VideoInfoResponse,
)
from app.service.video_downloader import VideoDownloaderService

router = APIRouter(prefix="/downloader", tags=["Video Downloader"])


@router.post(
    "/info",
    response_model=VideoInfoResponse,
    summary="Read a YouTube or Instagram link and list the qualities that can be downloaded",
    description=(
        "Nothing is downloaded or stored. The response has the title, the thumbnail and "
        "one choice per quality."
    ),
)
async def get_video_info(
    payload: VideoInfoRequest,
    current_user: CurrentUser,
) -> VideoInfoResponse:
    return await VideoDownloaderService().get_info(current_user.id, payload)


@router.post(
    "/prepare",
    response_model=PrepareDownloadResponse,
    summary="Get a short-lived download link for one quality",
    description=(
        "The browser opens GET /downloader/stream?token=... to start the download. "
        "A link cannot carry the login header, so it carries this token instead."
    ),
)
async def prepare_download(
    payload: PrepareDownloadRequest,
    current_user: CurrentUser,
) -> PrepareDownloadResponse:
    return await VideoDownloaderService().prepare(current_user.id, payload)


@router.get(
    "/stream",
    summary="Download the video (streamed through the server, never stored)",
    response_class=StreamingResponse,
)
async def stream_video(
    token: Annotated[str, Query(min_length=20, max_length=4000)],
) -> StreamingResponse:
    plan = await VideoDownloaderService().open_stream(token)
    headers = {
        "Content-Disposition": plan.disposition,
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    }
    if plan.content_length is not None:
        headers["Content-Length"] = str(plan.content_length)
    return StreamingResponse(plan.iterator, media_type=plan.media_type, headers=headers)
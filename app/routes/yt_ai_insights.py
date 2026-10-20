# app/routes/yt_ai_insights.py
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Path, Query, status

from app.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.schemas.yt_ai_insights import (
    AiReportListResponse,
    AiReportResponse,
    CreateAiReportRequest,
    CreatePortfolioReportRequest,
)
from app.service.yt_ai_insights import YtAiInsightsService
from app.utils.yt_ai_insights import AiReportType

router = APIRouter(prefix="/yt-analytics", tags=["YouTube AI Insights"])

VideoId = Annotated[str, Query(min_length=5, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]


@router.post(
    "/accounts/{account_id}/ai-reports",
    response_model=AiReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an AI report for one channel or one video",
    description=(
        "Report types: channel_strategy, content_analysis, publishing_schedule, "
        "audience_keywords, comments_insights, content_plan and video_review (needs video_id). "
        "The same data returns the saved report instead of using the AI again, unless force is "
        "true. Creating a report can take up to a minute."
    ),
)
async def create_ai_report(
    account_id: uuid.UUID,
    payload: CreateAiReportRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> AiReportResponse:
    return await YtAiInsightsService(session).create_report(current_user.id, account_id, payload)


@router.post(
    "/ai-reports/portfolio",
    response_model=AiReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an AI report that compares all your channels",
)
async def create_portfolio_report(
    payload: CreatePortfolioReportRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> AiReportResponse:
    return await YtAiInsightsService(session).create_portfolio_report(current_user.id, payload)


@router.get(
    "/ai-reports",
    response_model=AiReportListResponse,
    summary="Saved AI reports",
    description="Newest first. Filter by channel, video or report type.",
)
async def list_ai_reports(
    current_user: CurrentUser,
    session: DbSession,
    account_id: Annotated[Optional[uuid.UUID], Query()] = None,
    video_id: Optional[VideoId] = None,
    report_type: Annotated[Optional[AiReportType], Query()] = None,
    limit: Annotated[
        int, Query(ge=1, le=settings.yt_list_max_limit)
    ] = settings.yt_ai_list_default_limit,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AiReportListResponse:
    return await YtAiInsightsService(session).list_reports(
        current_user.id,
        account_id=account_id,
        video_id=video_id,
        report_type=report_type,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/ai-reports/{report_id}",
    response_model=AiReportResponse,
    summary="One saved AI report",
)
async def get_ai_report(
    report_id: Annotated[uuid.UUID, Path()],
    current_user: CurrentUser,
    session: DbSession,
) -> AiReportResponse:
    return await YtAiInsightsService(session).get_report(current_user.id, report_id)


@router.delete(
    "/ai-reports/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved AI report",
)
async def delete_ai_report(
    report_id: Annotated[uuid.UUID, Path()],
    current_user: CurrentUser,
    session: DbSession,
) -> None:
    await YtAiInsightsService(session).delete_report(current_user.id, report_id)
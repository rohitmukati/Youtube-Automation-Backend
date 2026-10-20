# app/crud/yt_ai_insights.py
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.yt_ai_insights import YtAiReport


def _scope_conditions(
    user_id: uuid.UUID,
    account_id: Optional[uuid.UUID],
    video_id: Optional[str],
    report_type: str,
) -> list:
    return [
        YtAiReport.user_id == user_id,
        YtAiReport.report_type == report_type,
        YtAiReport.account_id.is_(None) if account_id is None else YtAiReport.account_id == account_id,
        YtAiReport.video_id.is_(None) if video_id is None else YtAiReport.video_id == video_id,
    ]


async def create_report(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    account_id: Optional[uuid.UUID],
    video_id: Optional[str],
    report_type: str,
    period_days: int,
    language: str,
    model: str,
    input_hash: str,
    result: dict[str, Any],
    data_quality: dict[str, Any],
) -> YtAiReport:
    report = YtAiReport(
        user_id=user_id,
        account_id=account_id,
        video_id=video_id,
        report_type=report_type,
        period_days=period_days,
        language=language,
        model=model,
        input_hash=input_hash,
        result=result,
        data_quality=data_quality,
    )
    session.add(report)
    await session.flush()
    return report


async def find_cached(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    account_id: Optional[uuid.UUID],
    video_id: Optional[str],
    report_type: str,
    input_hash: str,
    since: datetime,
) -> Optional[YtAiReport]:
    result = await session.execute(
        select(YtAiReport)
        .where(
            *_scope_conditions(user_id, account_id, video_id, report_type),
            YtAiReport.input_hash == input_hash,
            YtAiReport.created_at >= since,
        )
        .order_by(YtAiReport.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_created_at(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    account_id: Optional[uuid.UUID],
    video_id: Optional[str],
    report_type: str,
) -> Optional[datetime]:
    result = await session.execute(
        select(func.max(YtAiReport.created_at)).where(
            *_scope_conditions(user_id, account_id, video_id, report_type)
        )
    )
    return result.scalar_one_or_none()


async def count_since(session: AsyncSession, user_id: uuid.UUID, since: datetime) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(YtAiReport)
        .where(YtAiReport.user_id == user_id, YtAiReport.created_at >= since)
    )
    return int(result.scalar_one())


async def get_report(
    session: AsyncSession, user_id: uuid.UUID, report_id: uuid.UUID
) -> Optional[YtAiReport]:
    result = await session.execute(
        select(YtAiReport).where(YtAiReport.id == report_id, YtAiReport.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_reports(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    account_id: Optional[uuid.UUID],
    video_id: Optional[str],
    report_type: Optional[str],
    limit: int,
    offset: int,
) -> tuple[list[YtAiReport], int]:
    conditions = [YtAiReport.user_id == user_id]
    if account_id is not None:
        conditions.append(YtAiReport.account_id == account_id)
    if video_id is not None:
        conditions.append(YtAiReport.video_id == video_id)
    if report_type is not None:
        conditions.append(YtAiReport.report_type == report_type)

    total = (
        await session.execute(select(func.count()).select_from(YtAiReport).where(*conditions))
    ).scalar_one()
    result = await session.execute(
        select(YtAiReport)
        .where(*conditions)
        .order_by(YtAiReport.created_at.desc(), YtAiReport.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total)


async def delete_report(session: AsyncSession, report: YtAiReport) -> None:
    await session.execute(delete(YtAiReport).where(YtAiReport.id == report.id))
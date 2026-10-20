# app/schemas/yt_ai_insights.py
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.utils.yt_ai_insights import AiLanguage, AiReportType

Level = Literal["high", "medium", "low"]
_TEXT_LIMIT = 1500


def _text(value: Any) -> Any:
    if value is None:
        return ""
    text = str(value).strip()
    return text if len(text) <= _TEXT_LIMIT else text[: _TEXT_LIMIT - 3] + "..."


def _level(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_text(value)] if value.strip() else []
    return [_text(item) for item in value if item is not None and str(item).strip()]


class AiInsight(BaseModel):
    title: str
    category: str = "other"
    finding: str = ""
    evidence: str = ""
    recommendation: str = ""
    action_steps: list[str] = Field(default_factory=list)
    priority: Level = "medium"
    impact: Level = "medium"
    effort: Level = "medium"
    confidence: Level = "medium"

    @field_validator("title", "category", "finding", "evidence", "recommendation", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        return _text(value)

    @field_validator("action_steps", mode="before")
    @classmethod
    def clean_steps(cls, value: Any) -> list[str]:
        return _text_list(value)

    @field_validator("priority", "impact", "effort", "confidence", mode="before")
    @classmethod
    def clean_level(cls, value: Any) -> Any:
        return _level(value)


class AiContentIdea(BaseModel):
    title: str
    format: str = "long"
    hook: str = ""
    why_it_should_work: str = ""
    based_on: str = ""

    @field_validator("title", "format", "hook", "why_it_should_work", "based_on", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        return _text(value)


class AiExperiment(BaseModel):
    hypothesis: str
    how_to_test: str = ""
    success_metric: str = ""

    @field_validator("hypothesis", "how_to_test", "success_metric", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        return _text(value)


class AiReportContent(BaseModel):
    headline: str
    summary: str = ""
    score: Optional[int] = None
    score_explanation: Optional[str] = None
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    insights: list[AiInsight] = Field(default_factory=list)
    quick_wins: list[str] = Field(default_factory=list)
    content_ideas: list[AiContentIdea] = Field(default_factory=list)
    experiments: list[AiExperiment] = Field(default_factory=list)
    data_limitations: list[str] = Field(default_factory=list)

    @field_validator("headline", "summary", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        return _text(value)

    @field_validator("score", mode="before")
    @classmethod
    def clean_score(cls, value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            return None

    @field_validator("score_explanation", mode="before")
    @classmethod
    def clean_explanation(cls, value: Any) -> Optional[str]:
        return _text(value) if value else None

    @field_validator("strengths", "weaknesses", "quick_wins", "data_limitations", mode="before")
    @classmethod
    def clean_lists(cls, value: Any) -> list[str]:
        return _text_list(value)

    @field_validator("insights", "content_ideas", "experiments", mode="before")
    @classmethod
    def clean_object_lists(cls, value: Any) -> list:
        if value is None:
            return []
        return [item for item in value if isinstance(item, dict)]


class CreateAiReportRequest(BaseModel):
    report_type: AiReportType
    days: int = Field(default=settings.yt_ai_default_days, ge=7, le=365)
    language: AiLanguage = AiLanguage.EN
    video_id: Optional[str] = Field(
        default=None, min_length=5, max_length=32, pattern=r"^[A-Za-z0-9_-]+$"
    )
    timezone: Optional[str] = Field(default=None, max_length=64)
    force: bool = False

    @model_validator(mode="after")
    def check_report_type(self) -> "CreateAiReportRequest":
        if self.report_type is AiReportType.PORTFOLIO:
            raise ValueError("Use the all-channels report endpoint for the portfolio report.")
        if self.report_type is AiReportType.VIDEO_REVIEW:
            if not self.video_id:
                raise ValueError("Pick a video for the video review.")
        else:
            self.video_id = None
        return self


class CreatePortfolioReportRequest(BaseModel):
    days: int = Field(default=settings.yt_ai_default_days, ge=7, le=365)
    language: AiLanguage = AiLanguage.EN
    force: bool = False


class AiReportResponse(BaseModel):
    id: uuid.UUID
    account_id: Optional[uuid.UUID] = None
    video_id: Optional[str] = None
    report_type: AiReportType
    period_days: int
    language: AiLanguage
    model: str
    created_at: datetime
    cached: bool = False
    content: AiReportContent
    data_quality: dict[str, Any]


class AiReportSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: Optional[uuid.UUID] = None
    video_id: Optional[str] = None
    report_type: AiReportType
    period_days: int
    language: AiLanguage
    model: str
    created_at: datetime
    headline: Optional[str] = None
    score: Optional[int] = None


class AiReportListResponse(BaseModel):
    reports: list[AiReportSummary]
    total: int
    limit: int
    offset: int
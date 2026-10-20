# app/utils/yt_analytics.py
import json
import re
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from enum import Enum
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.exceptions import (
    AnalyticsApiDisabledError,
    AnalyticsScopeMissingError,
    AppException,
    NotFoundError,
    ValidationError,
    YouTubeApiError,
    YouTubeQuotaError,
)
from app.utils.datetime_utils import ensure_utc, utc_now


class SyncStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ContentType(str, Enum):
    ALL = "all"
    SHORT = "short"
    LONG = "long"


class VideoSort(str, Enum):
    PUBLISHED_AT = "published_at"
    VIEWS = "views"
    LIKES = "likes"
    COMMENTS = "comments"
    ENGAGEMENT = "engagement"
    PERFORMANCE = "performance"
    DURATION = "duration"


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"


WEEKDAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_PARTS = (
    ("night", 0, 5),
    ("morning", 6, 11),
    ("afternoon", 12, 17),
    ("evening", 18, 23),
)
DURATION_BUCKETS = (
    ("up to 1 min", 0, 60),
    ("1 to 3 min", 60, 180),
    ("3 to 8 min", 180, 480),
    ("8 to 15 min", 480, 900),
    ("15 to 30 min", 900, 1800),
    ("over 30 min", 1800, 10**9),
)
TITLE_LENGTH_BUCKETS = (
    ("up to 30 characters", 0, 30),
    ("31 to 50 characters", 31, 50),
    ("51 to 70 characters", 51, 70),
    ("over 70 characters", 71, 10**6),
)
TAG_COUNT_BUCKETS = (
    ("no tags", 0, 0),
    ("1 to 5 tags", 1, 5),
    ("6 to 10 tags", 6, 10),
    ("11 to 20 tags", 11, 20),
    ("over 20 tags", 21, 10**6),
)

_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_EMOJI_PATTERN = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_NETWORK_MODULES = ("httplib2", "urllib3", "requests", "ssl", "socket")


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> "DateRange":
        previous_end = self.start - timedelta(days=1)
        return DateRange(start=previous_end - timedelta(days=self.days - 1), end=previous_end)


@dataclass
class VideoPoint:
    video_id: str
    title: str
    published_at: datetime
    duration_seconds: int
    is_short: bool
    views: int
    likes: Optional[int]
    comments: Optional[int]
    engagement_rate: float
    performance_index: Optional[float]
    tags: list[str] = field(default_factory=list)


def latest_data_date(today: Optional[date] = None) -> date:
    base = today or utc_now().date()
    return base - timedelta(days=settings.yt_analytics_data_delay_days)


def resolve_range(
    days: Optional[int],
    start_date: Optional[date],
    end_date: Optional[date],
    *,
    today: Optional[date] = None,
) -> DateRange:
    latest = latest_data_date(today)
    max_days = settings.yt_analytics_max_days

    if (start_date is None) != (end_date is None):
        raise ValidationError("Provide both a start date and an end date, or neither.")

    if start_date is not None and end_date is not None:
        if start_date > end_date:
            raise ValidationError("The start date must be on or before the end date.")
        end_date = min(end_date, latest)
        if start_date > end_date:
            raise ValidationError(
                "YouTube analytics is available up to "
                f"{settings.yt_analytics_data_delay_days} days ago. Pick an earlier date range."
            )
        if (end_date - start_date).days + 1 > max_days:
            raise ValidationError(f"The date range can be at most {max_days} days.")
        return DateRange(start=start_date, end=end_date)

    length = days or settings.yt_analytics_default_days
    length = max(1, min(length, max_days))
    return DateRange(start=latest - timedelta(days=length - 1), end=latest)


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_iso_duration(value: Optional[str]) -> int:
    if not value:
        return 0
    match = _DURATION_PATTERN.match(value)
    if not match:
        return 0
    parts = {key: int(number) if number else 0 for key, number in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def is_short_video(duration_seconds: int) -> bool:
    return 0 < duration_seconds <= settings.yt_analytics_short_max_seconds


def to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def engagement_rate(likes: Optional[int], comments: Optional[int], views: int) -> float:
    if not views:
        return 0.0
    return round(((likes or 0) + (comments or 0)) / views * 100, 2)


def percent_change(current: float, previous: float) -> Optional[float]:
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def share_percent(value: float, total: float) -> float:
    if not total:
        return 0.0
    return round(value / total * 100, 1)


def median_or_none(values: Sequence[float], digits: int = 2) -> Optional[float]:
    if not values:
        return None
    return round(statistics.median(values), digits)


def mean_or_none(values: Sequence[float], digits: int = 2) -> Optional[float]:
    if not values:
        return None
    return round(statistics.fmean(values), digits)


def confidence_for(sample_size: int) -> str:
    if sample_size >= 40:
        return "high"
    if sample_size >= 15:
        return "medium"
    if sample_size >= 1:
        return "low"
    return "none"


def performance_label(index: Optional[float]) -> str:
    if index is None:
        return "not_enough_data"
    if index >= 1.5:
        return "outperforming"
    if index <= 0.7:
        return "underperforming"
    return "typical"


def report_rows(report: Optional[dict]) -> list[dict[str, Any]]:
    if not report:
        return []
    columns = report.get("columns") or []
    return [dict(zip(columns, row)) for row in (report.get("rows") or [])]


def row_date(row: dict[str, Any]) -> Optional[date]:
    raw = row.get("day")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def split_daily_rows(
    rows: Sequence[dict[str, Any]], current: DateRange
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current_rows: list[dict[str, Any]] = []
    previous_rows: list[dict[str, Any]] = []
    for row in rows:
        day = row_date(row)
        if day is None:
            continue
        if current.start <= day <= current.end:
            current_rows.append(row)
        elif day < current.start:
            previous_rows.append(row)
    return current_rows, previous_rows


def summarize_daily(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    sums = {
        "views": 0.0,
        "estimatedMinutesWatched": 0.0,
        "likes": 0.0,
        "comments": 0.0,
        "shares": 0.0,
        "subscribersGained": 0.0,
        "subscribersLost": 0.0,
    }
    weighted_percentage = 0.0
    for row in rows:
        views = number(row.get("views"))
        for key in sums:
            sums[key] += number(row.get(key))
        weighted_percentage += number(row.get("averageViewPercentage")) * views

    views = sums["views"]
    minutes = sums["estimatedMinutesWatched"]
    gained = int(sums["subscribersGained"])
    lost = int(sums["subscribersLost"])
    return {
        "views": int(views),
        "estimated_minutes_watched": int(minutes),
        "watch_time_hours": round(minutes / 60, 1),
        "average_view_duration_seconds": round(minutes * 60 / views, 1) if views else 0.0,
        "average_view_percentage": round(weighted_percentage / views, 1) if views else 0.0,
        "likes": int(sums["likes"]),
        "comments": int(sums["comments"]),
        "shares": int(sums["shares"]),
        "subscribers_gained": gained,
        "subscribers_lost": lost,
        "net_subscribers": gained - lost,
        "engagement_rate": engagement_rate(int(sums["likes"]), int(sums["comments"]), int(views)),
    }


def totals_change(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Optional[float]]:
    return {
        key: percent_change(float(value), float(previous.get(key) or 0))
        for key, value in current.items()
    }


def daily_points(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for row in rows:
        views = number(row.get("views"))
        minutes = number(row.get("estimatedMinutesWatched"))
        points.append(
            {
                "date": str(row.get("day")),
                "views": int(views),
                "estimated_minutes_watched": int(minutes),
                "average_view_duration_seconds": round(minutes * 60 / views, 1) if views else 0.0,
                "average_view_percentage": round(number(row.get("averageViewPercentage")), 1),
                "likes": int(number(row.get("likes"))),
                "comments": int(number(row.get("comments"))),
                "shares": int(number(row.get("shares"))),
                "subscribers_gained": int(number(row.get("subscribersGained"))),
                "subscribers_lost": int(number(row.get("subscribersLost"))),
            }
        )
    points.sort(key=lambda point: point["date"])
    return points


def bucket_points(points: Sequence[dict[str, Any]], bucket_days: int = 7) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    for index in range(0, len(points), bucket_days):
        chunk = points[index : index + bucket_days]
        views = sum(point["views"] for point in chunk)
        minutes = sum(point["estimated_minutes_watched"] for point in chunk)
        buckets.append(
            {
                "from": chunk[0]["date"],
                "to": chunk[-1]["date"],
                "views": views,
                "watch_hours": round(minutes / 60, 1),
                "likes": sum(point["likes"] for point in chunk),
                "comments": sum(point["comments"] for point in chunk),
                "subscribers_net": sum(
                    point["subscribers_gained"] - point["subscribers_lost"] for point in chunk
                ),
            }
        )
    return buckets


def breakdown_rows(
    rows: Sequence[dict[str, Any]], key_field: str, *, value_field: str = "views"
) -> list[dict[str, Any]]:
    total = sum(number(row.get(value_field)) for row in rows)
    result = []
    for row in rows:
        views = number(row.get("views"))
        result.append(
            {
                "key": str(row.get(key_field)),
                "views": int(views),
                "estimated_minutes_watched": int(number(row.get("estimatedMinutesWatched"))),
                "share_percent": share_percent(number(row.get(value_field)), total),
            }
        )
    return result


def downsample(values: Sequence[Any], target: int) -> list[Any]:
    if len(values) <= target or target < 2:
        return list(values)
    step = (len(values) - 1) / (target - 1)
    return [values[round(index * step)] for index in range(target)]


def compute_performance_indexes(
    videos: Sequence[tuple[str, datetime, Optional[int]]],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Optional[float]]:
    current = now or utc_now()
    minimum_age = timedelta(days=settings.yt_analytics_min_video_age_days)
    window = timedelta(days=settings.yt_analytics_neighbor_window_days).total_seconds()
    result: dict[str, Optional[float]] = {video_id: None for video_id, _, _ in videos}

    eligible = sorted(
        (
            (ensure_utc(published).timestamp(), views, video_id)
            for video_id, published, views in videos
            if views is not None and current - ensure_utc(published) >= minimum_age
        ),
        key=lambda item: item[0],
    )
    if len(eligible) < settings.yt_analytics_neighbor_min:
        return result

    stamps = [item[0] for item in eligible]
    counts = [item[1] for item in eligible]
    overall = statistics.median(counts)

    for stamp, views, video_id in eligible:
        low = bisect_left(stamps, stamp - window)
        high = bisect_right(stamps, stamp + window)
        neighbors = list(counts[low:high])
        neighbors.remove(views)
        base = (
            statistics.median(neighbors)
            if len(neighbors) >= settings.yt_analytics_neighbor_min
            else overall
        )
        result[video_id] = round(views / base, 2) if base > 0 else None
    return result


def filter_points(points: Sequence[VideoPoint], content_type: ContentType) -> list[VideoPoint]:
    if content_type is ContentType.SHORT:
        return [point for point in points if point.is_short]
    if content_type is ContentType.LONG:
        return [point for point in points if not point.is_short]
    return list(points)


def _scored(points: Sequence[VideoPoint]) -> list[VideoPoint]:
    return [point for point in points if point.performance_index is not None]


def _group_summary(items: Sequence[VideoPoint]) -> dict[str, Any]:
    indexes = [point.performance_index for point in items if point.performance_index is not None]
    return {
        "videos": len(items),
        "median_performance_index": median_or_none(indexes),
        "median_views": int(statistics.median([point.views for point in items])) if items else 0,
    }


_TIMEZONE_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Dacca": "Asia/Dhaka",
    "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
    "Europe/Kiev": "Europe/Kyiv",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Pacific/Truk": "Pacific/Chuuk",
}


def _load_timezone(name: str) -> Optional[tzinfo]:
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def resolve_timezone(name: Optional[str]) -> tuple[tzinfo, str]:
    requested = (name or "").strip()
    candidates = [
        _TIMEZONE_ALIASES.get(requested, requested),
        settings.yt_analytics_default_timezone,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        zone = _load_timezone(candidate)
        if zone is not None:
            return zone, candidate
    return timezone.utc, "UTC"


def _best_of(rows: Sequence[dict[str, Any]], key: str) -> Optional[Any]:
    eligible = [
        row
        for row in rows
        if row["videos"] >= settings.yt_analytics_min_slot_videos
        and row["median_performance_index"] is not None
    ]
    if not eligible:
        return None
    best = max(eligible, key=lambda row: (row["median_performance_index"], row["videos"]))
    return best[key]


def build_publishing_insights(points: Sequence[VideoPoint], timezone_name: str) -> dict[str, Any]:
    tz, used_timezone = resolve_timezone(timezone_name)
    requested = _TIMEZONE_ALIASES.get((timezone_name or "").strip(), (timezone_name or "").strip())
    scored = _scored(points)

    by_weekday: dict[int, list[VideoPoint]] = defaultdict(list)
    by_hour: dict[int, list[VideoPoint]] = defaultdict(list)
    by_part: dict[str, list[VideoPoint]] = defaultdict(list)
    heatmap: dict[tuple[int, int], list[VideoPoint]] = defaultdict(list)

    for point in scored:
        local = ensure_utc(point.published_at).astimezone(tz)
        by_weekday[local.weekday()].append(point)
        by_hour[local.hour].append(point)
        heatmap[(local.weekday(), local.hour)].append(point)
        for name, first, last in DAY_PARTS:
            if first <= local.hour <= last:
                by_part[name].append(point)

    weekday_rows = [
        {"weekday": day, "label": WEEKDAY_LABELS[day], **_group_summary(by_weekday[day])}
        for day in range(7)
    ]
    hour_rows = [{"hour": hour, **_group_summary(by_hour[hour])} for hour in range(24)]
    part_rows = [
        {"day_part": name, "hours": f"{first:02d}:00-{last:02d}:59", **_group_summary(by_part[name])}
        for name, first, last in DAY_PARTS
    ]
    heatmap_rows = [
        {"weekday": day, "hour": hour, **_group_summary(items)}
        for (day, hour), items in sorted(heatmap.items())
    ]
    slot_candidates = [
        {
            "weekday": row["weekday"],
            "label": WEEKDAY_LABELS[row["weekday"]],
            "hour": row["hour"],
            "videos": row["videos"],
            "median_performance_index": row["median_performance_index"],
            "median_views": row["median_views"],
        }
        for row in heatmap_rows
        if row["videos"] >= settings.yt_analytics_min_slot_videos
        and row["median_performance_index"] is not None
    ]
    best_slots = sorted(
        slot_candidates, key=lambda row: (row["median_performance_index"], row["videos"]), reverse=True
    )[:3]

    best_weekday = _best_of(weekday_rows, "label")
    best_hour = _best_of(hour_rows, "hour")
    best_part = _best_of(part_rows, "day_part")

    sample = len(scored)
    if sample < settings.yt_analytics_min_videos_for_insights:
        note = (
            "There are too few videos with enough history to find a reliable upload time. "
            "Treat these numbers as a first hint and keep publishing."
        )
    else:
        note = (
            "Upload times are compared using each video's performance index, so a video is judged "
            "against other videos published around the same time. YouTube does not share when "
            "your viewers are online through its API."
        )

    if requested and used_timezone != requested:
        note += f" The timezone {requested} was not recognised, so {used_timezone} was used."

    return {
        "timezone": used_timezone,
        "sample_size": sample,
        "confidence": confidence_for(sample),
        "best_weekday": best_weekday,
        "best_hour": best_hour,
        "best_day_part": best_part,
        "best_slots": best_slots,
        "by_weekday": weekday_rows,
        "by_hour": hour_rows,
        "by_day_part": part_rows,
        "heatmap": heatmap_rows,
        "note": note,
    }


def _normalized_tags(point: VideoPoint) -> list[str]:
    return list(dict.fromkeys(tag.strip().lower() for tag in point.tags if tag and tag.strip()))


def _bucketed(
    points: Sequence[VideoPoint], buckets: Sequence[tuple[str, int, int]], value_of
) -> list[dict[str, Any]]:
    rows = []
    for label, low, high in buckets:
        items = [point for point in points if low <= value_of(point) <= high]
        rows.append({"label": label, **_group_summary(items)})
    return rows


def build_tag_insights(points: Sequence[VideoPoint]) -> dict[str, Any]:
    tag_map: dict[str, list[VideoPoint]] = defaultdict(list)
    for point in points:
        for tag in _normalized_tags(point):
            tag_map[tag].append(point)

    rows = []
    for tag, items in tag_map.items():
        indexes = [item.performance_index for item in items if item.performance_index is not None]
        rows.append(
            {
                "tag": tag,
                "videos": len(items),
                "scored_videos": len(indexes),
                "median_performance_index": median_or_none(indexes),
                "average_views": int(statistics.fmean([item.views for item in items])),
            }
        )

    eligible = [row for row in rows if row["scored_videos"] >= settings.yt_analytics_min_tag_videos]
    top = sorted(
        eligible,
        key=lambda row: (row["median_performance_index"], row["scored_videos"]),
        reverse=True,
    )[:15]
    top_names = {row["tag"] for row in top}
    weak = sorted(
        (row for row in eligible if row["tag"] not in top_names and row["median_performance_index"] < 1),
        key=lambda row: (row["median_performance_index"], -row["scored_videos"]),
    )[:10]
    most_used = sorted(rows, key=lambda row: (-row["videos"], row["tag"]))[:15]

    with_tags = [point for point in points if _normalized_tags(point)]
    return {
        "videos_with_tags": len(with_tags),
        "videos_without_tags": len(points) - len(with_tags),
        "average_tags_per_video": mean_or_none([len(_normalized_tags(point)) for point in points], 1),
        "top_tags": top,
        "weak_tags": weak,
        "most_used_tags": most_used,
        "tag_count_groups": _bucketed(points, TAG_COUNT_BUCKETS, lambda point: len(_normalized_tags(point))),
        "note": (
            "Tags only describe your own videos here. A tag is listed as strong or weak when it "
            f"appears on at least {settings.yt_analytics_min_tag_videos} videos with enough history."
        ),
    }


_TITLE_FEATURES = (
    ("has_number", lambda title: bool(re.search(r"\d", title))),
    ("has_question_mark", lambda title: "?" in title),
    ("has_brackets", lambda title: bool(re.search(r"[\[\(\{]", title))),
    ("has_emoji", lambda title: bool(_EMOJI_PATTERN.search(title))),
    (
        "has_all_caps_word",
        lambda title: any(len(word) >= 3 and word.isupper() for word in re.findall(r"[A-Za-z]+", title)),
    ),
    ("has_separator", lambda title: any(mark in title for mark in (" | ", " - ", ":"))),
    ("has_hashtag", lambda title: "#" in title),
)


def build_title_insights(points: Sequence[VideoPoint]) -> dict[str, Any]:
    scored = _scored(points)
    minimum = settings.yt_analytics_min_group_videos
    features = []
    for name, predicate in _TITLE_FEATURES:
        with_feature = [point.performance_index for point in scored if predicate(point.title)]
        without_feature = [point.performance_index for point in scored if not predicate(point.title)]
        if len(with_feature) < minimum or len(without_feature) < minimum:
            continue
        with_median = statistics.median(with_feature)
        without_median = statistics.median(without_feature)
        features.append(
            {
                "feature": name,
                "videos_with": len(with_feature),
                "videos_without": len(without_feature),
                "median_index_with": round(with_median, 2),
                "median_index_without": round(without_median, 2),
                "lift": round(with_median / without_median, 2) if without_median else None,
            }
        )
    features.sort(key=lambda row: row["lift"] if row["lift"] is not None else 0, reverse=True)
    return {
        "average_title_length": mean_or_none([len(point.title) for point in points], 1),
        "length_groups": _bucketed(scored, TITLE_LENGTH_BUCKETS, lambda point: len(point.title)),
        "features": features,
    }


def build_duration_insights(points: Sequence[VideoPoint]) -> list[dict[str, Any]]:
    return _bucketed(_scored(points), DURATION_BUCKETS, lambda point: point.duration_seconds)


def build_cadence_insights(points: Sequence[VideoPoint], *, now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or utc_now()
    ordered = sorted((ensure_utc(point.published_at) for point in points))
    gaps = [
        (later - earlier).total_seconds() / 86400
        for earlier, later in zip(ordered, ordered[1:])
    ]
    last_30 = sum(1 for stamp in ordered if current - stamp <= timedelta(days=30))
    previous_30 = sum(
        1 for stamp in ordered if timedelta(days=30) < current - stamp <= timedelta(days=60)
    )
    monthly: dict[str, int] = defaultdict(int)
    for stamp in ordered:
        if current - stamp <= timedelta(days=365):
            monthly[stamp.strftime("%Y-%m")] += 1
    return {
        "total_videos": len(ordered),
        "first_published_at": ordered[0].isoformat() if ordered else None,
        "last_published_at": ordered[-1].isoformat() if ordered else None,
        "days_since_last_upload": (current - ordered[-1]).days if ordered else None,
        "uploads_last_30_days": last_30,
        "uploads_previous_30_days": previous_30,
        "median_days_between_uploads": median_or_none(gaps, 1),
        "uploads_per_month": [
            {"month": month, "videos": count} for month, count in sorted(monthly.items())
        ],
    }


def video_summary(point: VideoPoint, *, with_tags: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "video_id": point.video_id,
        "title": point.title,
        "published_at": ensure_utc(point.published_at).isoformat(),
        "duration_seconds": point.duration_seconds,
        "is_short": point.is_short,
        "views": point.views,
        "likes": point.likes,
        "comments": point.comments,
        "engagement_rate": point.engagement_rate,
        "performance_index": point.performance_index,
    }
    if with_tags:
        summary["tags"] = point.tags[:10]
    return summary


def rank_videos(points: Sequence[VideoPoint], count: int, *, with_tags: bool = False) -> dict[str, list]:
    scored = _scored(points)
    best = sorted(scored, key=lambda point: point.performance_index, reverse=True)[:count]
    worst = sorted(scored, key=lambda point: point.performance_index)[:count]
    return {
        "best": [video_summary(point, with_tags=with_tags) for point in best],
        "worst": [video_summary(point, with_tags=with_tags) for point in worst],
    }


def youtube_error_reason(exc: BaseException) -> str:
    content = getattr(exc, "content", None)
    if not content:
        return ""
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason", ""))
    return str(error.get("status", ""))


def youtube_error_status(exc: BaseException) -> Optional[int]:
    raw = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def classify_youtube_error(exc: BaseException) -> AppException:
    if isinstance(exc, AppException):
        return exc

    status = youtube_error_status(exc)
    if status is not None and hasattr(exc, "content"):
        reason = youtube_error_reason(exc)
        if reason in {"quotaExceeded", "dailyLimitExceeded"}:
            return YouTubeQuotaError()
        if reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
            return AnalyticsApiDisabledError()
        if reason == "insufficientPermissions":
            return AnalyticsScopeMissingError()
        if status == 401 or reason in {"authError", "forbiddenForUserAuth"}:
            return ValidationError(
                "This YouTube account permission has expired. Please reconnect the account."
            )
        if status == 404 or reason in {"videoNotFound", "channelNotFound"}:
            return NotFoundError("This item could not be found on YouTube.")
        if status == 400:
            return YouTubeApiError(
                "YouTube could not build this report for the selected dates. Try a different date range.",
                code="youtube_bad_request",
            )
        if status == 403:
            return YouTubeApiError(
                "YouTube refused this request for the channel. Check the channel permissions and try again.",
                code="youtube_forbidden",
            )
        return YouTubeApiError()

    module = type(exc).__module__ or ""
    if isinstance(exc, (OSError, TimeoutError)) or module.startswith(_NETWORK_MODULES):
        return YouTubeApiError("The connection to YouTube was interrupted. Please try again.")
    return YouTubeApiError()


def comments_disabled(exc: BaseException) -> bool:
    return youtube_error_reason(exc) in {"commentsDisabled", "forbidden"} and youtube_error_status(exc) == 403


def clip(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 3] + "..."


def report_cache_key(name: str, *parts: Any) -> str:
    return ":".join([name, *[str(part) for part in parts]])[:255]
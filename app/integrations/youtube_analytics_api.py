# app/integrations/youtube_analytics_api.py
import logging
from datetime import date
from typing import Any, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings
from app.utils.yt_analytics import (
    is_short_video,
    parse_iso_datetime,
    parse_iso_duration,
    to_int,
)

logger = logging.getLogger(__name__)

_VIDEO_BATCH_SIZE = 50


def _youtube(credentials: Credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def _analytics(credentials: Credentials):
    return build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)


def _thumbnail_url(snippet: dict) -> Optional[str]:
    thumbnails = snippet.get("thumbnails", {})
    for size in ("maxres", "standard", "high", "medium", "default"):
        url = thumbnails.get(size, {}).get("url")
        if url:
            return url
    return None


def fetch_channel_sync(credentials: Credentials, channel_id: str) -> dict[str, Any]:
    response = (
        _youtube(credentials)
        .channels()
        .list(part="snippet,statistics,contentDetails", id=channel_id)
        .execute()
    )
    items = response.get("items", [])
    if not items:
        return {}
    channel = items[0]
    snippet = channel.get("snippet", {})
    statistics = channel.get("statistics", {})
    hidden = bool(statistics.get("hiddenSubscriberCount", False))
    return {
        "channel_id": channel["id"],
        "title": snippet.get("title"),
        "description": snippet.get("description", ""),
        "country": snippet.get("country"),
        "thumbnail_url": _thumbnail_url(snippet),
        "uploads_playlist_id": channel.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads"),
        "subscribers": None if hidden else to_int(statistics.get("subscriberCount")),
        "hidden_subscriber_count": hidden,
        "total_views": to_int(statistics.get("viewCount")) or 0,
        "video_count": to_int(statistics.get("videoCount")) or 0,
    }


def list_upload_video_ids_sync(
    credentials: Credentials, playlist_id: str, *, max_videos: int
) -> tuple[list[str], bool]:
    youtube = _youtube(credentials)
    video_ids: list[str] = []
    page_token: Optional[str] = None
    truncated = False
    while True:
        response = (
            youtube.playlistItems()
            .list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=settings.yt_analytics_page_size,
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                video_ids.append(video_id)
        page_token = response.get("nextPageToken")
        if len(video_ids) >= max_videos:
            truncated = bool(page_token) or len(video_ids) > max_videos
            video_ids = video_ids[:max_videos]
            break
        if not page_token:
            break
    return list(dict.fromkeys(video_ids)), truncated


def _normalize_video(item: dict) -> Optional[dict[str, Any]]:
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    published_at = parse_iso_datetime(snippet.get("publishedAt"))
    if published_at is None:
        return None
    duration = parse_iso_duration(item.get("contentDetails", {}).get("duration"))
    views = to_int(statistics.get("viewCount")) or 0
    return {
        "video_id": item["id"],
        "title": (snippet.get("title") or "Untitled")[:255],
        "description": snippet.get("description", ""),
        "tags": [str(tag) for tag in snippet.get("tags", [])],
        "category_id": snippet.get("categoryId"),
        "default_language": snippet.get("defaultLanguage") or snippet.get("defaultAudioLanguage"),
        "published_at": published_at,
        "duration_seconds": duration,
        "is_short": is_short_video(duration),
        "privacy_status": item.get("status", {}).get("privacyStatus", "public"),
        "thumbnail_url": _thumbnail_url(snippet),
        "view_count": views,
        "like_count": to_int(statistics.get("likeCount")),
        "comment_count": to_int(statistics.get("commentCount")),
        "live_broadcast_content": snippet.get("liveBroadcastContent", "none"),
    }


def fetch_videos_sync(credentials: Credentials, video_ids: list[str]) -> list[dict[str, Any]]:
    youtube = _youtube(credentials)
    videos: list[dict[str, Any]] = []
    for start in range(0, len(video_ids), _VIDEO_BATCH_SIZE):
        batch = video_ids[start : start + _VIDEO_BATCH_SIZE]
        response = (
            youtube.videos()
            .list(
                part="snippet,statistics,contentDetails,status",
                id=",".join(batch),
                maxResults=_VIDEO_BATCH_SIZE,
            )
            .execute()
        )
        for item in response.get("items", []):
            normalized = _normalize_video(item)
            if normalized is not None:
                videos.append(normalized)
    return videos


def fetch_comments_sync(
    credentials: Credentials, video_id: str, *, limit: int
) -> list[dict[str, Any]]:
    youtube = _youtube(credentials)
    comments: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    while len(comments) < limit:
        response = (
            youtube.commentThreads()
            .list(
                part="snippet",
                videoId=video_id,
                order="relevance",
                textFormat="plainText",
                maxResults=min(100, limit - len(comments)),
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("items", []):
            thread = item.get("snippet", {})
            top = thread.get("topLevelComment", {}).get("snippet", {})
            text = (top.get("textDisplay") or "").strip()
            if not text:
                continue
            comments.append(
                {
                    "text": text[:1000],
                    "like_count": to_int(top.get("likeCount")) or 0,
                    "reply_count": to_int(thread.get("totalReplyCount")) or 0,
                    "published_at": top.get("publishedAt"),
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return comments[:limit]


def query_report_sync(
    credentials: Credentials,
    *,
    start_date: date,
    end_date: date,
    metrics: str,
    dimensions: Optional[str] = None,
    filters: Optional[str] = None,
    sort: Optional[str] = None,
    max_results: Optional[int] = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": metrics,
    }
    if dimensions:
        params["dimensions"] = dimensions
    if filters:
        params["filters"] = filters
    if sort:
        params["sort"] = sort
    if max_results:
        params["maxResults"] = max_results

    response = _analytics(credentials).reports().query(**params).execute()
    columns = [header.get("name") for header in response.get("columnHeaders", [])]
    return {"columns": columns, "rows": response.get("rows") or []}
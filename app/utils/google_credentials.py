# app/utils/google_credentials.py
import json
import re

from app.config import settings

_CLIENT_ID_PATTERN = re.compile(settings.google_client_id_pattern)


def clean_client_id(value: str) -> str:
    cleaned = value.strip()
    if not _CLIENT_ID_PATTERN.match(cleaned):
        raise ValueError(
            "The Client ID is not valid. It looks like 1234567890-abc123.apps.googleusercontent.com."
        )
    return cleaned


def clean_client_secret(value: str) -> str:
    cleaned = value.strip()
    if (
        len(cleaned) < settings.google_client_secret_min_length
        or len(cleaned) > settings.google_client_secret_max_length
        or any(char.isspace() for char in cleaned)
    ):
        raise ValueError("The Client Secret is not valid. Copy it again from the Google Cloud console.")
    return cleaned


COMMENTS_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPE_LABELS = {
    "https://www.googleapis.com/auth/youtube.upload": "Upload videos to your YouTube channel",
    "https://www.googleapis.com/auth/youtube.readonly": "View your YouTube account and channel data",
    "https://www.googleapis.com/auth/youtube.force-ssl": "Read the comments on your videos",
    "https://www.googleapis.com/auth/yt-analytics.readonly": "View YouTube Analytics reports",
}
API_DATA_NAME = "YouTube Data API v3"
API_ANALYTICS_NAME = "YouTube Analytics API"
API_LIBRARY_URLS = {
    API_DATA_NAME: "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
    API_ANALYTICS_NAME: "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com",
}
_API_DISABLED_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}


def parse_granted_scopes(value) -> list[str]:
    if not value:
        return []
    items = value.split() if isinstance(value, str) else list(value)
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def missing_required_scopes(granted) -> list[str]:
    granted_set = set(parse_granted_scopes(granted))
    return [scope for scope in settings.google_required_scopes if scope not in granted_set]


def scopes_missing_message(missing: list[str]) -> str:
    labels = "; ".join(SCOPE_LABELS.get(scope, scope) for scope in missing)
    return (
        f"These permissions were not granted: {labels}. "
        "Connect the channel again and keep every permission ticked on the Google screen, "
        "or choose Select all. If a permission is not shown on that screen at all, add it to "
        "the scopes of your OAuth consent screen in Google Cloud and try again."
    )


def apis_not_enabled_message(api_names: list[str]) -> str:
    names = ", ".join(api_names)
    plural = "are" if len(api_names) > 1 else "is"
    return (
        f"{names} {plural} not enabled in your Google Cloud project. "
        "Open the Google Cloud console, go to APIs and Services, then Library, search for each "
        "API, click Enable, wait a minute and connect the channel again."
    )


def google_error_reasons(exc: BaseException) -> set[str]:
    content = getattr(exc, "content", None)
    if not content:
        return set()
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return set()
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return set()

    reasons: set[str] = set()
    for item in error.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.add(str(item["reason"]))
    for item in error.get("details") or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.add(str(item["reason"]))
    if error.get("status"):
        reasons.add(str(error["status"]))
    return reasons


def is_api_disabled_error(exc: BaseException) -> bool:
    if google_error_reasons(exc) & _API_DISABLED_REASONS:
        return True
    return "has not been used in project" in str(getattr(exc, "content", b"")).lower()


def is_permission_error(exc: BaseException) -> bool:
    return "insufficientPermissions" in google_error_reasons(exc)


def api_activation_url(exc: BaseException, api_name: str) -> str:
    content = getattr(exc, "content", None)
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content or "")
        data = json.loads(text)
        for item in (data.get("error") or {}).get("details") or []:
            url = (item.get("metadata") or {}).get("activationUrl") if isinstance(item, dict) else None
            if url:
                return str(url)
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass
    return API_LIBRARY_URLS[api_name]
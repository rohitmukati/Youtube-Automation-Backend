# app/utils/google_credentials.py
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

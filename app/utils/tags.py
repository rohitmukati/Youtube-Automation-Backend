from typing import Iterable, Optional

from app.config import settings


def normalize_tags(values: Iterable[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = " ".join(value.split()).lower()
        if not tag:
            continue
        if "," in tag or any(ord(char) < 32 for char in tag):
            raise ValueError("Tags cannot contain commas or control characters.")
        if len(tag) > settings.storage_max_tag_length:
            raise ValueError(
                f"Each tag can be at most {settings.storage_max_tag_length} characters long."
            )
        if tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    if len(tags) > settings.storage_max_tags:
        raise ValueError(f"A file can have at most {settings.storage_max_tags} tags.")
    return tags


def split_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return normalize_tags(raw.split(","))
import logging
import os
import re
import uuid
from pathlib import PurePath
from typing import Optional

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import AppException, NotFoundError, ValidationError
from app.crud import storage_bucket as storage_crud
from app.models.storage_bucket import StorageFile, StorageFileType
from app.schemas.storage_bucket import StorageFileListResponse, StorageFileResponse
from app.utils import bucket
from app.utils.tags import normalize_tags, split_tags

logger = logging.getLogger(__name__)

_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9]{1,10}$")
_BYTES_PER_MB = 1024 * 1024


def _classify(content_type: str) -> Optional[StorageFileType]:
    if content_type in settings.storage_video_content_types:
        return StorageFileType.VIDEO
    if content_type in settings.storage_image_content_types:
        return StorageFileType.IMAGE
    return None


def _max_bytes(file_type: StorageFileType) -> int:
    if file_type is StorageFileType.VIDEO:
        return settings.storage_video_max_bytes
    return settings.storage_image_max_bytes


def _measure(upload: UploadFile) -> int:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def _clean_name(raw: Optional[str]) -> str:
    name = PurePath((raw or "").replace("\\", "/")).name.strip()
    return name[:255] or "file"


def _extension(name: str) -> str:
    suffix = PurePath(name).suffix.lower()
    return suffix if _EXTENSION_PATTERN.match(suffix) else ""


def _parse_tags(raw: Optional[str]) -> list[str]:
    try:
        return split_tags(raw)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _to_response(record: StorageFile, signed_url: Optional[str]) -> StorageFileResponse:
    return StorageFileResponse(
        id=record.id,
        file_type=StorageFileType(record.file_type),
        original_name=record.original_name,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        tags=sorted(item.tag for item in record.tags),
        signed_url=signed_url,
        created_at=record.created_at,
    )


async def _sign_one(path: str) -> Optional[str]:
    try:
        return await bucket.create_signed_url(path)
    except AppException:
        return None


async def _sign_many(paths: list[str]) -> dict[str, str]:
    try:
        return await bucket.create_signed_urls(paths)
    except AppException:
        return {}


async def _delete_quietly(path: str) -> None:
    try:
        await bucket.delete_object(path)
    except AppException:
        logger.warning("Could not remove stored object %s during cleanup", path)


class StorageBucketService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upload_file(
        self, user_id: uuid.UUID, upload: UploadFile, tags_raw: Optional[str]
    ) -> StorageFileResponse:
        content_type = (upload.content_type or "").split(";")[0].strip().lower()
        file_type = _classify(content_type)
        if file_type is None:
            raise ValidationError(
                "Only video files (MP4, MOV, AVI, MKV, WEBM, MPEG) and "
                "image files (JPG, PNG, WEBP, GIF) can be uploaded."
            )

        size = _measure(upload)
        if size == 0:
            raise ValidationError("The uploaded file is empty.")
        limit = _max_bytes(file_type)
        if size > limit:
            raise ValidationError(
                f"{file_type.value.capitalize()} files can be at most {limit // _BYTES_PER_MB} MB."
            )

        tags = _parse_tags(tags_raw)
        original_name = _clean_name(upload.filename)
        file_id = uuid.uuid4()
        storage_path = f"{user_id}/{file_type.value}s/{file_id}{_extension(original_name)}"

        await bucket.ensure_bucket()
        await bucket.upload_object(storage_path, upload.file, content_type, size)

        try:
            record = await storage_crud.create_file(
                self._session,
                file_id=file_id,
                user_id=user_id,
                file_type=file_type,
                original_name=original_name,
                content_type=content_type,
                size_bytes=size,
                storage_path=storage_path,
                tags=tags,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            await _delete_quietly(storage_path)
            raise

        logger.info("Stored %s %s for user %s", file_type.value, file_id, user_id)
        return _to_response(record, await _sign_one(storage_path))

    async def list_files(
        self,
        user_id: uuid.UUID,
        *,
        file_type: Optional[StorageFileType],
        tags_raw: Optional[str],
        search: Optional[str],
        limit: int,
        offset: int,
    ) -> StorageFileListResponse:
        records, total = await storage_crud.list_files(
            self._session,
            user_id=user_id,
            file_type=file_type,
            tags=_parse_tags(tags_raw),
            search=(search or "").strip() or None,
            limit=limit,
            offset=offset,
        )
        urls = await _sign_many([record.storage_path for record in records])
        return StorageFileListResponse(
            files=[_to_response(record, urls.get(record.storage_path)) for record in records],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def update_tags(
        self, user_id: uuid.UUID, file_id: uuid.UUID, tags: list[str]
    ) -> StorageFileResponse:
        record = await self._get_file(user_id, file_id)
        await storage_crud.set_file_tags(self._session, record, normalize_tags(tags))
        await self._session.commit()
        return _to_response(record, await _sign_one(record.storage_path))

    async def delete_file(self, user_id: uuid.UUID, file_id: uuid.UUID) -> None:
        record = await self._get_file(user_id, file_id)
        await bucket.delete_object(record.storage_path)
        await storage_crud.delete_file(self._session, record)
        await self._session.commit()
        logger.info("Deleted file %s for user %s", file_id, user_id)

    async def _get_file(self, user_id: uuid.UUID, file_id: uuid.UUID) -> StorageFile:
        record = await storage_crud.get_file(self._session, file_id, user_id)
        if record is None:
            raise NotFoundError("File not found.")
        return record
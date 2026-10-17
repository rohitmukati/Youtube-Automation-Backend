import uuid
from typing import Optional

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_bucket import StorageFile, StorageFileTag, StorageFileType
from app.utils.datetime_utils import utc_now


def _apply_filters(
    statement: Select,
    *,
    user_id: uuid.UUID,
    file_type: Optional[StorageFileType],
    tags: list[str],
    search: Optional[str],
) -> Select:
    statement = statement.where(StorageFile.user_id == user_id)
    if file_type is not None:
        statement = statement.where(StorageFile.file_type == file_type.value)
    for tag in tags:
        statement = statement.where(
            StorageFile.id.in_(
                select(StorageFileTag.file_id).where(StorageFileTag.tag == tag)
            )
        )
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(
            StorageFile.original_name.ilike(f"%{escaped}%", escape="\\")
        )
    return statement


async def create_file(
    session: AsyncSession,
    *,
    file_id: uuid.UUID,
    user_id: uuid.UUID,
    file_type: StorageFileType,
    original_name: str,
    content_type: str,
    size_bytes: int,
    storage_path: str,
    tags: list[str],
) -> StorageFile:
    record = StorageFile(
        id=file_id,
        user_id=user_id,
        file_type=file_type.value,
        original_name=original_name,
        content_type=content_type,
        size_bytes=size_bytes,
        storage_path=storage_path,
        tags=[StorageFileTag(tag=tag) for tag in tags],
    )
    session.add(record)
    await session.flush()
    return record


async def list_files(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    file_type: Optional[StorageFileType],
    tags: list[str],
    search: Optional[str],
    limit: int,
    offset: int,
) -> tuple[list[StorageFile], int]:
    filters = {"user_id": user_id, "file_type": file_type, "tags": tags, "search": search}
    total = await session.scalar(
        _apply_filters(select(func.count()).select_from(StorageFile), **filters)
    )
    result = await session.execute(
        _apply_filters(select(StorageFile), **filters)
        .order_by(StorageFile.created_at.desc(), StorageFile.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def get_file(
    session: AsyncSession, file_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[StorageFile]:
    result = await session.execute(
        select(StorageFile).where(
            StorageFile.id == file_id,
            StorageFile.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def set_file_tags(
    session: AsyncSession, record: StorageFile, tags: list[str]
) -> StorageFile:
    existing = {item.tag: item for item in record.tags}
    wanted = set(tags)
    for tag, item in existing.items():
        if tag not in wanted:
            record.tags.remove(item)
    for tag in tags:
        if tag not in existing:
            record.tags.append(StorageFileTag(tag=tag))
    record.updated_at = utc_now()
    await session.flush()
    return record


async def delete_file(session: AsyncSession, record: StorageFile) -> None:
    await session.delete(record)
    await session.flush()
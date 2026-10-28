# app/routes/storage_bucket.py
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.schemas.storage_bucket import (
    MessageResponse,
    StorageFileListResponse,
    StorageFileResponse,
    UpdateTagsRequest,
)
from app.service.storage_bucket import StorageBucketService

router = APIRouter(prefix="/storage", tags=["Storage Bucket"])


@router.post(
    "/files",
    response_model=StorageFileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an image to the library",
    description=(
        "Send an image (JPG, PNG, WEBP or GIF) as multipart form data. The content "
        "of the file is checked, not only its name. Tags are optional and comma "
        "separated, for example: thumbnail, summer. Videos are not stored in the "
        "library: they are uploaded when you publish them."
    ),
)
async def upload_file(
    current_user: CurrentUser,
    session: DbSession,
    file: Annotated[UploadFile, File()],
    tags: Annotated[Optional[str], Form()] = None,
) -> StorageFileResponse:
    return await StorageBucketService(session).upload_file(
        user_id=current_user.id,
        upload=file,
        tags_raw=tags,
    )


@router.get(
    "/files",
    response_model=StorageFileListResponse,
    summary="List the images in the library",
    description=(
        "Returns images with temporary signed URLs. Filter by tags (comma "
        "separated, an image must have all of them) and by file name."
    ),
)
async def list_files(
    current_user: CurrentUser,
    session: DbSession,
    tags: Annotated[Optional[str], Query()] = None,
    search: Annotated[Optional[str], Query(max_length=100)] = None,
    limit: Annotated[
        int, Query(ge=1, le=settings.storage_list_max_limit)
    ] = settings.storage_list_default_limit,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StorageFileListResponse:
    return await StorageBucketService(session).list_files(
        user_id=current_user.id,
        tags_raw=tags,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/files/{file_id}",
    response_model=StorageFileResponse,
    summary="Replace the tags of a file",
)
async def update_tags(
    file_id: uuid.UUID,
    payload: UpdateTagsRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> StorageFileResponse:
    return await StorageBucketService(session).update_tags(
        user_id=current_user.id,
        file_id=file_id,
        tags=payload.tags,
    )


@router.delete(
    "/files/{file_id}",
    response_model=MessageResponse,
    summary="Delete an image",
)
async def delete_file(
    file_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> MessageResponse:
    await StorageBucketService(session).delete_file(
        user_id=current_user.id,
        file_id=file_id,
    )
    return MessageResponse(message="The file was deleted.")
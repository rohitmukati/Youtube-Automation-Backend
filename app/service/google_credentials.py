# app/service/google_credentials.py
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    GoogleCredentialsMissingError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.crud import google_credentials as crud
from app.schemas.google_credentials import GoogleCredentialsResponse
from app.utils.token_encryption import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

_UNREADABLE_MESSAGE = (
    "Your saved Google credentials could not be read. Save your Client ID and Client Secret again."
)


@dataclass(frozen=True)
class GoogleClient:
    client_id: str
    client_secret: str


async def load_client(session: AsyncSession, user_id: uuid.UUID) -> GoogleClient:
    record = await crud.get_by_user(session, user_id)
    if record is None:
        raise GoogleCredentialsMissingError()
    try:
        secret = decrypt_token(record.encrypted_client_secret)
    except RuntimeError as exc:
        logger.error("Saved Google client secret of user %s could not be decrypted", user_id)
        raise GoogleCredentialsMissingError(_UNREADABLE_MESSAGE) from exc
    return GoogleClient(client_id=record.client_id, client_secret=secret)


async def ensure_configured(session: AsyncSession, user_id: uuid.UUID) -> None:
    if await crud.get_by_user(session, user_id) is None:
        raise GoogleCredentialsMissingError()


def _status(record) -> GoogleCredentialsResponse:
    if record is None:
        return GoogleCredentialsResponse(
            configured=False,
            redirect_uri=settings.google_redirect_uri,
        )
    return GoogleCredentialsResponse(
        configured=True,
        client_id=record.client_id,
        client_secret_saved=True,
        redirect_uri=settings.google_redirect_uri,
        updated_at=record.updated_at,
    )


class GoogleCredentialsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_status(self, user_id: uuid.UUID) -> GoogleCredentialsResponse:
        return _status(await crud.get_by_user(self._session, user_id))

    async def save(
        self, user_id: uuid.UUID, *, client_id: str, client_secret: str
    ) -> GoogleCredentialsResponse:
        if not settings.token_encryption_key:
            raise ServiceUnavailableError(
                "Secure storage is not configured on the server. Please contact support."
            )
        encrypted_secret = encrypt_token(client_secret)

        for attempt in range(2):
            record = await crud.get_by_user(self._session, user_id)
            client_changed = record is not None and record.client_id != client_id
            try:
                if record is None:
                    record = await crud.create(
                        self._session,
                        user_id=user_id,
                        client_id=client_id,
                        encrypted_client_secret=encrypted_secret,
                    )
                else:
                    await crud.update_record(
                        self._session,
                        record,
                        client_id=client_id,
                        encrypted_client_secret=encrypted_secret,
                    )
                if client_changed:
                    await crud.flag_user_accounts_need_reconnect(self._session, user_id)
                await self._session.commit()
                break
            except IntegrityError:
                await self._session.rollback()
                if attempt == 1:
                    raise
        logger.info("Google credentials saved for user %s", user_id)
        return _status(record)

    async def delete(self, user_id: uuid.UUID) -> None:
        record = await crud.get_by_user(self._session, user_id)
        if record is None:
            raise NotFoundError("No Google credentials are saved for your account.")
        await crud.flag_user_accounts_need_reconnect(self._session, user_id)
        await crud.delete(self._session, record)
        await self._session.commit()
        logger.info("Google credentials deleted for user %s", user_id)

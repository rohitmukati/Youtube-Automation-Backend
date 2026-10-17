import uuid
from typing import Annotated, Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import AccountDisabledError, AuthenticationError, InvalidTokenError
from app.core.security import decode_access_token
from app.crud import auth as auth_crud
from app.integrations.smtp_email import SmtpEmailSender
from app.models.auth import User
from app.service.email import EmailService

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_email_service() -> EmailService:
    return EmailService(SmtpEmailSender())


async def get_current_user(
    session: DbSession,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Please log in to continue.", code="not_authenticated")

    payload = decode_access_token(credentials.credentials)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (ValueError, KeyError) as exc:
        raise InvalidTokenError() from exc

    user = await auth_crud.get_user_by_id(session, user_id)
    if user is None:
        raise InvalidTokenError()
    if not user.is_active:
        raise AccountDisabledError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

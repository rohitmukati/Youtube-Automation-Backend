import logging
import uuid
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Query, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.core.exceptions import AppException
from app.schemas.yt_management import (
    AccountListResponse,
    ConnectAccountRequest,
    ConnectInitiateResponse,
    ConnectSuccessResponse,
    MessageResponse,
    RenameAccountRequest,
    YouTubeAccountResponse,
)
from app.service.yt_management import YouTubeManagementService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/youtube", tags=["YouTube Management"])


def _frontend_redirect(**params: str) -> RedirectResponse:
    query = urlencode(params)
    return RedirectResponse(url=f"{settings.frontend_url}/youtube/connect?{query}")


@router.post(
    "/connect",
    response_model=ConnectInitiateResponse,
    summary="Step 1 — YouTube account connect karo",
    description=(
        "Apne YouTube account ko ek nickname do. "
        "Response mein Google OAuth URL milega — "
        "frontend us URL par user ko redirect kare."
    ),
)
async def initiate_connect(
    payload: ConnectAccountRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> ConnectInitiateResponse:
    return await YouTubeManagementService(session).initiate_connect(
        user_id=current_user.id,
        nickname=payload.nickname,
    )


@router.get(
    "/callback",
    summary="Step 2 — Google OAuth callback (Google is URL par redirect karta hai)",
    description=(
        "Yeh endpoint sirf Google use karta hai. "
        "Frontend yahan directly hit nahi karta."
    ),
)
async def oauth_callback(
    session: DbSession,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    """
    Google callback ke 2 possible outcomes:
    - Success → frontend ke success page par redirect
    - Failure → frontend ke error page par redirect
    """
    if error or not code or not state:
        return _frontend_redirect(status="error", reason=error or "missing_params")

    try:
        result = await YouTubeManagementService(session).handle_callback(
            code=code,
            state_token=state,
            error=error,
        )
    except AppException as exc:
        return _frontend_redirect(status="error", reason=exc.message)
    except Exception:
        logger.exception("Unexpected error in YouTube OAuth callback")
        return _frontend_redirect(
            status="error",
            reason="Something went wrong while connecting the account. Please try again.",
        )

    return _frontend_redirect(
        status="success",
        channel=result.account.channel_title,
        account_id=str(result.account.id),
    )


@router.get(
    "/accounts",
    response_model=AccountListResponse,
    summary="Saare connected YouTube accounts dekho",
)
async def list_accounts(
    current_user: CurrentUser,
    session: DbSession,
) -> AccountListResponse:
    return await YouTubeManagementService(session).list_accounts(current_user.id)


@router.patch(
    "/accounts/{account_id}",
    response_model=YouTubeAccountResponse,
    summary="Account ka nickname badlo",
)
async def rename_account(
    account_id: uuid.UUID,
    payload: RenameAccountRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> YouTubeAccountResponse:
    return await YouTubeManagementService(session).rename_account(
        user_id=current_user.id,
        account_id=account_id,
        nickname=payload.nickname,
    )


@router.delete(
    "/accounts/{account_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="YouTube account disconnect karo",
)
async def disconnect_account(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> MessageResponse:
    await YouTubeManagementService(session).disconnect_account(
        user_id=current_user.id,
        account_id=account_id,
    )
    return MessageResponse(message="YouTube account successfully disconnect ho gaya.")
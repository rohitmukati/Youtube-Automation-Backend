# app/routes/yt_management.py
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
    UpdateAccountRequest,
    YouTubeAccountResponse,
)
from app.service.yt_management import YouTubeManagementService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/youtube", tags=["YouTube Account Management"])


def _frontend_redirect(**params: str) -> RedirectResponse:
    query = urlencode(params)
    return RedirectResponse(url=f"{settings.frontend_url}/youtube/connect?{query}")


@router.post(
    "/connect",
    response_model=ConnectInitiateResponse,
    summary="Step 1 — Connect a YouTube account",
    description=(
        "Give your YouTube account a nickname. "
        "The response contains a Google OAuth URL — "
        "the frontend redirects the user to that URL."
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
    summary="Step 2 — Google OAuth callback",
    description=(
        "This endpoint is used only by Google. "
        "The frontend does not call it directly."
    ),
)
async def oauth_callback(
    session: DbSession,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    """
    Google callback has two possible outcomes:
    - Success → redirect to the frontend success page
    - Failure → redirect to the frontend error page
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
    summary="View all connected YouTube accounts",
)
async def list_accounts(
    current_user: CurrentUser,
    session: DbSession,
) -> AccountListResponse:
    return await YouTubeManagementService(session).list_accounts(current_user.id)


@router.post(
    "/accounts/refresh",
    response_model=AccountListResponse,
    summary="Refresh all accounts' email, channel name, and photo from Google",
    description=(
        "Older connected accounts may not have their Google email saved yet. "
        "Running this once fills in the email for all accounts."
    ),
)
async def refresh_all_accounts(
    current_user: CurrentUser,
    session: DbSession,
) -> AccountListResponse:
    return await YouTubeManagementService(session).refresh_all_accounts(current_user.id)


@router.post(
    "/accounts/{account_id}/refresh",
    response_model=YouTubeAccountResponse,
    summary="Refresh a single account's email, channel name, and photo from Google",
)
async def refresh_account(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    session: DbSession,
) -> YouTubeAccountResponse:
    return await YouTubeManagementService(session).refresh_account(
        user_id=current_user.id,
        account_id=account_id,
    )


@router.patch(
    "/accounts/{account_id}",
    response_model=YouTubeAccountResponse,
    summary="Update an account's nickname or notes",
    description=(
        "Only the fields you send will be updated. "
        "Notes can be up to 500 characters. Sending null or blank notes removes them."
    ),
)
async def update_account(
    account_id: uuid.UUID,
    payload: UpdateAccountRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> YouTubeAccountResponse:
    return await YouTubeManagementService(session).update_account(
        user_id=current_user.id,
        account_id=account_id,
        nickname=payload.nickname,
        notes=payload.notes,
        update_notes="notes" in payload.model_fields_set,
    )


@router.delete(
    "/accounts/{account_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Disconnect a YouTube account",
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
    return MessageResponse(message="YouTube account disconnected successfully.")
# app/routes/google_credentials.py
from fastapi import APIRouter

from app.core.dependencies import CurrentUser, DbSession
from app.schemas.google_credentials import (
    GoogleCredentialsResponse,
    MessageResponse,
    SaveGoogleCredentialsRequest,
)
from app.service.google_credentials import GoogleCredentialsService

router = APIRouter(prefix="/youtube/credentials", tags=["YouTube Google Credentials"])


@router.get(
    "",
    response_model=GoogleCredentialsResponse,
    summary="See whether your Google Client ID and Client Secret are saved",
    description=(
        "Returns your saved Client ID and the redirect URI that must be added to your "
        "OAuth client in the Google Cloud console. The Client Secret is never returned."
    ),
)
async def get_credentials(
    current_user: CurrentUser,
    session: DbSession,
) -> GoogleCredentialsResponse:
    return await GoogleCredentialsService(session).get_status(current_user.id)


@router.put(
    "",
    response_model=GoogleCredentialsResponse,
    summary="Save your Google Client ID and Client Secret",
    description=(
        "Every user connects channels and uploads through their own Google Cloud project. "
        "Create an OAuth client of type Web application with YouTube Data API v3 enabled, "
        "add the redirect URI shown by this API, then save its Client ID and Client Secret here. "
        "Changing the Client ID means every connected channel has to be connected again."
    ),
)
async def save_credentials(
    payload: SaveGoogleCredentialsRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> GoogleCredentialsResponse:
    return await GoogleCredentialsService(session).save(
        current_user.id,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
    )


@router.delete(
    "",
    response_model=MessageResponse,
    summary="Remove your saved Google Client ID and Client Secret",
    description="Your connected channels will need to be connected again after you save new credentials.",
)
async def delete_credentials(
    current_user: CurrentUser,
    session: DbSession,
) -> MessageResponse:
    await GoogleCredentialsService(session).delete(current_user.id)
    return MessageResponse(message="Your Google credentials were removed.")

# app/service/yt_management.py
import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    AccountDisabledError,
    AppException,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.crud import yt_management as yt_crud
from app.models.yt_management import YouTubeAccount
from app.schemas.yt_management import (
    AccountListResponse,
    ConnectInitiateResponse,
    ConnectSuccessResponse,
    YouTubeAccountResponse,
)
from app.utils.datetime_utils import ensure_utc, utc_now
from app.utils.token_encryption import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

# OAuth state is valid for 10 minutes only
_OAUTH_STATE_TTL_MINUTES = 10

# Refresh the access token slightly before it expires
_TOKEN_REFRESH_SKEW_SECONDS = 60

_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def _build_flow() -> Flow:
    """
    Builds a Google OAuth flow.
    A CLIENT_SECRETS file is not required — we use client_id/secret directly.
    """
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=settings.google_scopes,
        redirect_uri=settings.google_redirect_uri,
        autogenerate_code_verifier=False,
    )
    return flow


def _credentials_from_account(account: YouTubeAccount) -> Credentials:
    """Builds decrypted credentials from the database."""
    return Credentials(
        token=decrypt_token(account.encrypted_access_token),
        refresh_token=decrypt_token(account.encrypted_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        scopes=account.scopes.split(" "),
    )


def _expiry_of(credentials: Credentials) -> datetime:
    if credentials.expiry:
        return ensure_utc(credentials.expiry)
    return utc_now() + timedelta(hours=1)


def _exchange_code_sync(code: str) -> Credentials:
    flow = _build_flow()
    flow.fetch_token(code=code)
    return flow.credentials


def _fetch_channel_info_sync(credentials: Credentials) -> dict:
    """
    Fetches the basic channel information from the YouTube API.
    Returns: {channel_id, channel_title, thumbnail_url}
    """
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    response = youtube.channels().list(part="snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise ValidationError("This Google account is not linked to any YouTube channel.")

    channel = items[0]
    snippet = channel.get("snippet", {})
    thumbnails = snippet.get("thumbnails", {})
    thumbnail_url = (
        thumbnails.get("default", {}).get("url")
        or thumbnails.get("medium", {}).get("url")
    )
    return {
        "channel_id": channel["id"],
        "channel_title": snippet.get("title", "Unknown Channel"),
        "thumbnail_url": thumbnail_url,
    }


def _fetch_email_sync(credentials: Credentials) -> Optional[str]:
    try:
        response = requests.get(
            settings.google_userinfo_url,
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=settings.google_userinfo_timeout_seconds,
        )
        if response.status_code != 200:
            logger.warning("Google userinfo returned status %s", response.status_code)
            return None
        email = response.json().get("email")
        return email.strip().lower() if isinstance(email, str) and email.strip() else None
    except (requests.RequestException, ValueError):
        logger.warning("Could not read the Google account email", exc_info=True)
        return None


def _fetch_profile_sync(credentials: Credentials) -> dict:
    info = _fetch_channel_info_sync(credentials)
    info["email"] = _fetch_email_sync(credentials)
    return info


def _refresh_credentials_sync(credentials: Credentials) -> None:
    credentials.refresh(Request())


def _revoke_token_sync(token: str) -> None:
    response = requests.post(_REVOKE_URL, params={"token": token}, timeout=5)
    if response.status_code != 200:
        logger.warning("Google token revoke returned status %s", response.status_code)


class YouTubeManagementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _check_google_configured(self) -> None:
        if not settings.google_client_id or not settings.google_client_secret:
            raise ValidationError(
                "Google OAuth credentials are not configured. "
                "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the .env file."
            )
        if not settings.token_encryption_key:
            raise ValidationError(
                "TOKEN_ENCRYPTION_KEY is not set in the .env file. "
                "This is required to encrypt tokens."
            )

    async def initiate_connect(
        self, user_id: uuid.UUID, nickname: str
    ) -> ConnectInitiateResponse:
        """
        Step 1: the user provides a nickname → a Google OAuth URL is generated.
        The frontend redirects the user to this URL.
        """
        self._check_google_configured()

        # Remove expired states
        await yt_crud.cleanup_expired_oauth_states(self._session)

        # Create a random state token (CSRF protection)
        state_token = secrets.token_urlsafe(32)

        # Save the state in the database (valid for 10 minutes)
        await yt_crud.create_oauth_state(
            self._session,
            state_token=state_token,
            user_id=user_id,
            nickname=nickname,
            expires_at=utc_now() + timedelta(minutes=_OAUTH_STATE_TTL_MINUTES),
        )
        await self._session.commit()

        # Build the Google OAuth URL
        flow = _build_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline",  # A refresh token will be provided
            # select_account: show the account chooser each time so the user can choose a different Gmail account
            # consent: request consent each time (new refresh token)
            prompt="select_account consent",
            include_granted_scopes="false",
            state=state_token,
        )

        logger.info("OAuth flow started for user %s with nickname '%s'", user_id, nickname)
        return ConnectInitiateResponse(auth_url=auth_url)

    async def handle_callback(
        self,
        code: str,
        state_token: str,
        error: Optional[str] = None,
    ) -> ConnectSuccessResponse:
        """
        Step 2: Google sends the code + state on callback.
        Exchange tokens, fetch channel info, and save it to the database.
        """
        self._check_google_configured()

        # Did the user deny permission?
        if error:
            raise ValidationError(f"Google denied permission: {error}")

        # Verify the state (CSRF check)
        state_record = await yt_crud.get_oauth_state(self._session, state_token)
        if state_record is None:
            raise ValidationError("Invalid or expired OAuth state. Please try again.")

        if ensure_utc(state_record.expires_at) <= utc_now():
            await yt_crud.delete_oauth_state(self._session, state_token)
            await self._session.commit()
            raise ValidationError("The OAuth session expired (10 minutes). Please connect again.")

        user_id = state_record.user_id
        nickname = state_record.nickname

        # The state is single-use: it should not be used again even if something fails later
        await yt_crud.delete_oauth_state(self._session, state_token)
        await self._session.commit()

        try:
            # Google calls are blocking, so run them in a thread
            credentials = await asyncio.to_thread(_exchange_code_sync, code)
            channel_info = await asyncio.to_thread(_fetch_channel_info_sync, credentials)
        except AppException:
            raise
        except Exception:
            logger.exception("Google OAuth callback failed for user %s", user_id)
            raise ValidationError(
                "The Google account could not be connected. Please try again."
            )

        google_email = await asyncio.to_thread(_fetch_email_sync, credentials)

        channel_id = channel_info["channel_id"]
        token_expires_at = _expiry_of(credentials)
        scopes = " ".join(credentials.scopes or settings.google_scopes)

        existing = await yt_crud.get_account_by_channel_id(
            self._session, user_id, channel_id
        )

        refresh_token = credentials.refresh_token
        if not refresh_token and existing is not None:
            still_valid = (
                existing.is_active
                and not existing.needs_reconnect
                and bool(existing.encrypted_refresh_token)
            )
            if still_valid:
                refresh_token = decrypt_token(existing.encrypted_refresh_token)
        if not refresh_token:
            raise ValidationError(
                "Google did not grant offline access. Remove this app in your Google account settings and connect again."
            )

        if existing is not None:
            # Reconnect = refresh tokens, scopes, and channel info
            logger.info(
                "Re-connecting existing channel %s for user %s", channel_id, user_id
            )
            await yt_crud.reconnect_account(
                self._session,
                existing,
                nickname=nickname,
                channel_title=channel_info["channel_title"],
                channel_thumbnail_url=channel_info.get("thumbnail_url"),
                encrypted_access_token=encrypt_token(credentials.token),
                encrypted_refresh_token=encrypt_token(refresh_token),
                token_expires_at=token_expires_at,
                scopes=scopes,
                google_email=google_email,
            )
            await self._session.commit()
            return ConnectSuccessResponse(
                message=f"'{channel_info['channel_title']}' successfully reconnected!",
                account=YouTubeAccountResponse.model_validate(existing),
            )

        # Save the new account
        try:
            account = await yt_crud.create_youtube_account(
                self._session,
                user_id=user_id,
                nickname=nickname,
                channel_id=channel_id,
                channel_title=channel_info["channel_title"],
                channel_thumbnail_url=channel_info.get("thumbnail_url"),
                encrypted_access_token=encrypt_token(credentials.token),
                encrypted_refresh_token=encrypt_token(refresh_token),
                token_expires_at=token_expires_at,
                scopes=scopes,
                google_email=google_email,
            )
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError(
                "This channel is already connected. Refresh the page and check again."
            )

        logger.info(
            "YouTube account '%s' connected for user %s", channel_info["channel_title"], user_id
        )
        return ConnectSuccessResponse(
            message=f"'{channel_info['channel_title']}' successfully connected!",
            account=YouTubeAccountResponse.model_validate(account),
        )

    async def list_accounts(self, user_id: uuid.UUID) -> AccountListResponse:
        """
        All connected YouTube accounts for the user.
        Accounts that need reconnection have needs_reconnect = True.
        """
        accounts = await yt_crud.get_accounts_by_user(self._session, user_id)
        active = [a for a in accounts if a.is_active]
        return AccountListResponse(
            accounts=[YouTubeAccountResponse.model_validate(a) for a in active],
            total=len(active),
        )

    async def update_account(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        nickname: Optional[str],
        notes: Optional[str],
        update_notes: bool,
    ) -> YouTubeAccountResponse:
        account = await self._get_active_account(user_id, account_id)
        updated = await yt_crud.update_account_details(
            self._session,
            account,
            nickname=nickname,
            notes=notes,
            update_notes=update_notes,
        )
        await self._session.commit()
        return YouTubeAccountResponse.model_validate(updated)

    async def refresh_account(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> YouTubeAccountResponse:
        account = await self._get_active_account(user_id, account_id)
        if account.needs_reconnect:
            raise ValidationError(
                "This channel needs to be reconnected before its details can be refreshed."
            )
        if not await self._refresh_profile(user_id, account):
            raise ValidationError(
                "The details of this channel could not be refreshed right now. Please try again."
            )
        return YouTubeAccountResponse.model_validate(account)

    async def refresh_all_accounts(self, user_id: uuid.UUID) -> AccountListResponse:
        accounts = await yt_crud.get_accounts_by_user(self._session, user_id)
        active = [a for a in accounts if a.is_active]
        for account in active:
            if not account.needs_reconnect:
                await self._refresh_profile(user_id, account)
        return AccountListResponse(
            accounts=[YouTubeAccountResponse.model_validate(a) for a in active],
            total=len(active),
        )

    async def _refresh_profile(self, user_id: uuid.UUID, account: YouTubeAccount) -> bool:
        try:
            credentials = await self.get_fresh_credentials(user_id, account.id)
            profile = await asyncio.to_thread(_fetch_profile_sync, credentials)
        except AppException:
            logger.warning("Could not refresh the details of account %s", account.id)
            return False
        except Exception:
            logger.exception("Refreshing the details of account %s failed", account.id)
            return False

        same_channel = profile["channel_id"] == account.channel_id
        await yt_crud.update_account_profile(
            self._session,
            account,
            google_email=profile.get("email"),
            channel_title=profile["channel_title"] if same_channel else None,
            channel_thumbnail_url=profile.get("thumbnail_url") if same_channel else None,
        )
        await self._session.commit()
        return True

    async def disconnect_account(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> None:
        """
        Disconnect the account:
        1. Revoke the token on Google
        2. Clear the tokens in the DB and set is_active = False
        """
        account = await self._get_active_account(user_id, account_id)

        # Try to revoke the refresh token on Google
        try:
            refresh_token = decrypt_token(account.encrypted_refresh_token)
            await asyncio.to_thread(_revoke_token_sync, refresh_token)
        except Exception:
            # If revocation fails, still remove it from the database
            logger.warning(
                "Could not revoke Google token for account %s, deactivating anyway.",
                account_id,
            )

        await yt_crud.deactivate_account(self._session, account)
        await self._session.commit()
        logger.info("YouTube account %s disconnected for user %s", account_id, user_id)

    async def get_fresh_credentials(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> Credentials:
        """
        Use this during video upload.
        If the access token expires, it is automatically refreshed.
        """
        account = await self._get_active_account(user_id, account_id)

        if account.needs_reconnect:
            raise ValidationError(
                "This YouTube account permission has expired. Please reconnect the account."
            )

        credentials = _credentials_from_account(account)

        refresh_deadline = utc_now() + timedelta(seconds=_TOKEN_REFRESH_SKEW_SECONDS)
        if ensure_utc(account.token_expires_at) <= refresh_deadline:
            try:
                await asyncio.to_thread(_refresh_credentials_sync, credentials)
            except RefreshError:
                # Refresh token expired/revoked — the account list will show that reconnection is needed
                await yt_crud.mark_needs_reconnect(self._session, account)
                await self._session.commit()
                raise ValidationError(
                    "This YouTube account permission has expired. Please reconnect the account."
                )
            await yt_crud.update_account_tokens(
                self._session,
                account,
                encrypted_access_token=encrypt_token(credentials.token),
                token_expires_at=_expiry_of(credentials),
            )
            await self._session.commit()

        return credentials

    async def _get_active_account(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> YouTubeAccount:
        account = await yt_crud.get_account_by_id(
            self._session, account_id, user_id
        )
        if account is None:
            raise NotFoundError("YouTube account not found.")
        if not account.is_active:
            raise AccountDisabledError(
                "This YouTube account has been disconnected. Please connect it again."
            )
        return account
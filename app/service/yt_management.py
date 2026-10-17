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

# OAuth state valid rehta hai sirf 10 minute
_OAUTH_STATE_TTL_MINUTES = 10

# Access token expire hone se thoda pehle hi refresh kar lo
_TOKEN_REFRESH_SKEW_SECONDS = 60

_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def _build_flow() -> Flow:
    """
    Google OAuth flow banata hai.
    CLIENT_SECRETS file nahi chahiye — directly client_id/secret use karte hain.
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
    """DB se decrypted credentials banata hai."""
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
    YouTube API se channel ki basic info fetch karta hai.
    Returns: {channel_id, channel_title, thumbnail_url}
    """
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    response = youtube.channels().list(part="snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise ValidationError("Is Google account ke saath koi YouTube channel linked nahi hai.")

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
                "Google OAuth credentials configure nahi hain. "
                ".env mein GOOGLE_CLIENT_ID aur GOOGLE_CLIENT_SECRET set karo."
            )
        if not settings.token_encryption_key:
            raise ValidationError(
                "TOKEN_ENCRYPTION_KEY .env mein set nahi hai. "
                "Tokens encrypt karne ke liye yeh zaruri hai."
            )

    async def initiate_connect(
        self, user_id: uuid.UUID, nickname: str
    ) -> ConnectInitiateResponse:
        """
        Step 1: User nickname deta hai → Google OAuth URL generate hota hai.
        Frontend is URL par user ko redirect karega.
        """
        self._check_google_configured()

        # Purane expire states hata do
        await yt_crud.cleanup_expired_oauth_states(self._session)

        # Ek random state token banao (CSRF protection)
        state_token = secrets.token_urlsafe(32)

        # State DB mein save karo (10 min valid)
        await yt_crud.create_oauth_state(
            self._session,
            state_token=state_token,
            user_id=user_id,
            nickname=nickname,
            expires_at=utc_now() + timedelta(minutes=_OAUTH_STATE_TTL_MINUTES),
        )
        await self._session.commit()

        # Google OAuth URL banao
        flow = _build_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline",  # Refresh token milega
            # select_account: har baar account chooser dikhao, taaki alag Gmail chuna ja sake
            # consent: har baar consent maango (naya refresh token)
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
        Step 2: Google callback par code + state aata hai.
        Tokens exchange karo, channel info fetch karo, DB mein save karo.
        """
        self._check_google_configured()

        # User ne permission deny ki?
        if error:
            raise ValidationError(f"Google ne permission deny ki: {error}")

        # State verify karo (CSRF check)
        state_record = await yt_crud.get_oauth_state(self._session, state_token)
        if state_record is None:
            raise ValidationError("Invalid ya expired OAuth state. Dobara try karo.")

        if ensure_utc(state_record.expires_at) <= utc_now():
            await yt_crud.delete_oauth_state(self._session, state_token)
            await self._session.commit()
            raise ValidationError("OAuth session expire ho gaya (10 min). Phir se connect karo.")

        user_id = state_record.user_id
        nickname = state_record.nickname

        # State single-use hai: aage kuch fail ho tab bhi dobara use nahi hona chahiye
        await yt_crud.delete_oauth_state(self._session, state_token)
        await self._session.commit()

        try:
            # Google ke calls blocking hain, isliye thread mein chalate hain
            credentials = await asyncio.to_thread(_exchange_code_sync, code)
            channel_info = await asyncio.to_thread(_fetch_channel_info_sync, credentials)
        except AppException:
            raise
        except Exception:
            logger.exception("Google OAuth callback failed for user %s", user_id)
            raise ValidationError(
                "Google account connect nahi ho paaya. Kripya dobara try karo."
            )

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
                "Google ne offline access nahi diya. Google account settings se is app ka "
                "access hata kar dobara connect karo."
            )

        if existing is not None:
            # Re-connect = tokens, scopes aur channel info sab refresh karo
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
            )
            await self._session.commit()
            return ConnectSuccessResponse(
                message=f"'{channel_info['channel_title']}' successfully reconnected!",
                account=YouTubeAccountResponse.model_validate(existing),
            )

        # Naya account save karo
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
            )
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError(
                "Yeh channel pehle se connect ho chuka hai. Page refresh karke dekho."
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
        User ke saare connected YouTube accounts.
        Jin accounts ko reconnect chahiye unme needs_reconnect = True aata hai.
        """
        accounts = await yt_crud.get_accounts_by_user(self._session, user_id)
        active = [a for a in accounts if a.is_active]
        return AccountListResponse(
            accounts=[YouTubeAccountResponse.model_validate(a) for a in active],
            total=len(active),
        )

    async def rename_account(
        self, user_id: uuid.UUID, account_id: uuid.UUID, nickname: str
    ) -> YouTubeAccountResponse:
        account = await self._get_active_account(user_id, account_id)
        updated = await yt_crud.update_account_nickname(
            self._session, account, nickname
        )
        await self._session.commit()
        return YouTubeAccountResponse.model_validate(updated)

    async def disconnect_account(
        self, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> None:
        """
        Account disconnect karo:
        1. Google par token revoke karo
        2. DB mein tokens clear karo, is_active = False
        """
        account = await self._get_active_account(user_id, account_id)

        # Google par refresh token revoke karne ki koshish karo
        try:
            refresh_token = decrypt_token(account.encrypted_refresh_token)
            await asyncio.to_thread(_revoke_token_sync, refresh_token)
        except Exception:
            # Revoke fail ho toh bhi DB se hata do
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
        Video upload ke time use karo.
        Agar access token expire ho gaya hai toh automatically refresh karta hai.
        """
        account = await self._get_active_account(user_id, account_id)

        if account.needs_reconnect:
            raise ValidationError(
                "YouTube account ki permission expire ho gayi hai. Kripya account dobara connect karo."
            )

        credentials = _credentials_from_account(account)

        refresh_deadline = utc_now() + timedelta(seconds=_TOKEN_REFRESH_SKEW_SECONDS)
        if ensure_utc(account.token_expires_at) <= refresh_deadline:
            try:
                await asyncio.to_thread(_refresh_credentials_sync, credentials)
            except RefreshError:
                # Refresh token expire/revoked — account list mein "reconnect needed" dikhega
                await yt_crud.mark_needs_reconnect(self._session, account)
                await self._session.commit()
                raise ValidationError(
                    "YouTube account ki permission expire ho gayi. Kripya account dobara connect karo."
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
            raise NotFoundError("YouTube account nahi mila.")
        if not account.is_active:
            raise AccountDisabledError(
                "Yeh YouTube account disconnect ho chuka hai. Dobara connect karo."
            )
        return account
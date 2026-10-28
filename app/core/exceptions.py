# app/core/exceptions.py
from typing import Any, Dict, List, Optional


class AppException(Exception):
    status_code: int = 500
    code: str = "internal_error"
    message: str = "Something went wrong on our side. Please try again later."

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        details: Optional[List[Dict[str, Any]]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details
        self.headers = headers
        super().__init__(self.message)


class BadRequestError(AppException):
    status_code = 400
    code = "bad_request"
    message = "The request could not be processed. Please check your input and try again."


class ValidationError(BadRequestError):
    code = "validation_error"
    message = "Some of the information you entered is not valid. Please review it and try again."


class AuthenticationError(AppException):
    status_code = 401
    code = "authentication_failed"
    message = "Authentication is required to access this resource."

    def __init__(self, message=None, *, code=None, details=None, headers=None) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            headers=headers or {"WWW-Authenticate": "Bearer"},
        )


class PermissionDeniedError(AppException):
    status_code = 403
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(AppException):
    status_code = 404
    code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppException):
    status_code = 409
    code = "conflict"
    message = "This action conflicts with the current state of the resource."


class TooManyAttemptsError(AppException):
    status_code = 429
    code = "too_many_attempts"
    message = "Too many attempts. Please request a new code and try again."


class ServiceUnavailableError(AppException):
    status_code = 503
    code = "service_unavailable"
    message = "The service is temporarily unavailable. Please try again in a moment."


class InvalidCredentialsError(AuthenticationError):
    code = "invalid_credentials"
    message = "Invalid email or password."


class TokenExpiredError(AuthenticationError):
    code = "token_expired"
    message = "Your session has expired. Please log in again."


class InvalidTokenError(AuthenticationError):
    code = "invalid_token"
    message = "Your session is not valid. Please log in again."


class GoogleCredentialsMissingError(BadRequestError):
    code = "google_credentials_missing"
    message = (
        "Save your Google Client ID and Client Secret first, then connect or reconnect your channels."
    )


class GoogleCredentialsInvalidError(GoogleCredentialsMissingError):
    code = "google_credentials_invalid"
    message = (
        "Google rejected your saved Client ID or Client Secret. Save the correct values and try again."
    )


class AccountDisabledError(PermissionDeniedError):
    code = "account_disabled"
    message = "Your account has been disabled. Please contact support."


class EmailAlreadyRegisteredError(ConflictError):
    code = "email_already_registered"
    message = "An account with this email already exists. Please log in instead."


class InvalidOtpError(BadRequestError):
    code = "invalid_otp"
    message = "The verification code is invalid or has expired. Please request a new one."


class EmailNotConfiguredError(ServiceUnavailableError):
    code = "email_not_configured"
    message = "The email service is not available right now. Please try again later."


class EmailDeliveryError(ServiceUnavailableError):
    code = "email_delivery_failed"
    message = "We could not send the email right now. Please try again in a few minutes."



class AnalyticsScopeMissingError(BadRequestError):
    code = "analytics_permission_missing"
    message = (
        "This channel was connected before analytics was available. "
        "Reconnect the channel to unlock the analytics dashboard."
    )


class AnalyticsApiDisabledError(BadRequestError):
    code = "youtube_api_not_enabled"
    message = (
        "A required YouTube API is not enabled in your Google Cloud project. "
        "Enable the YouTube Data API v3 and the YouTube Analytics API, then try again."
    )


class AnalyticsNotSyncedError(BadRequestError):
    code = "analytics_not_synced"
    message = "The data of this channel has not been loaded yet. Start a sync first."


class YouTubeQuotaError(AppException):
    status_code = 429
    code = "youtube_quota_exceeded"
    message = (
        "The daily YouTube API limit of your Google project has been reached. "
        "It resets at midnight Pacific Time."
    )


class YouTubeApiError(ServiceUnavailableError):
    code = "youtube_unavailable"
    message = "YouTube did not answer properly. Please try again in a moment."


class AiNotConfiguredError(ServiceUnavailableError):
    code = "ai_not_configured"
    message = "AI insights are not available right now. Please contact support."


class AiGenerationError(ServiceUnavailableError):
    code = "ai_generation_failed"
    message = "The AI report could not be generated right now. Please try again in a few minutes."


class AiLimitReachedError(TooManyAttemptsError):
    code = "ai_limit_reached"
    message = "You have reached the AI report limit. Please try again later."


class CommentsPermissionMissingError(BadRequestError):
    code = "comments_permission_missing"
    message = (
        "Reading comments needs one more permission. "
        "Reconnect this channel and keep every permission ticked on the Google screen."
    )


class GoogleScopesMissingError(BadRequestError):
    code = "google_scopes_missing"
    message = (
        "Some required Google permissions were not granted. "
        "Connect the channel again and keep every permission ticked."
    )


class GoogleApiNotEnabledError(BadRequestError):
    code = "google_api_not_enabled"
    message = (
        "A required Google API is not enabled in your Google Cloud project. "
        "Enable it in the API Library and connect again."
    )


class UploadSessionGoneError(NotFoundError):
    code = "upload_session_gone"
    message = "This upload is no longer available. Please start the upload again."


class UploadIncompleteError(ValidationError):
    code = "upload_incomplete"
    message = "The upload is not complete yet. Some parts are missing or have the wrong size."


class MediaNotReadyError(ConflictError):
    code = "media_not_ready"
    message = "The video file is still being uploaded."


class MediaExpiredError(ConflictError):
    code = "media_expired"
    message = (
        "The video file was removed from our servers after 24 hours. "
        "Start a new upload with the file to publish it again."
    )


class StagingBusyError(ServiceUnavailableError):
    code = "staging_busy"
    message = "Uploads are very busy right now. Please try again in a few minutes."
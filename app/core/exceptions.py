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

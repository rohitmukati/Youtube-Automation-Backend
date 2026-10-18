# app/integrations/google_oauth.py
import logging
from enum import Enum

import requests

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_CHECK_TIMEOUT_SECONDS = 10
_PROBE_CODE = "credential-check"
_PROBE_REDIRECT_URI = "http://localhost"

# Google authenticates the OAuth client before it looks at the authorization code, so these
# answers mean the Client ID and Client Secret themselves were accepted.
_CLIENT_ACCEPTED_ERRORS = {"invalid_grant", "redirect_uri_mismatch"}


class ClientCheck(str, Enum):
    VALID = "valid"
    CLIENT_NOT_FOUND = "client_not_found"
    SECRET_MISMATCH = "secret_mismatch"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


def check_client_sync(client_id: str, client_secret: str, redirect_uri: str) -> ClientCheck:
    """Ask Google whether this Client ID and Client Secret belong together.

    A token request with a deliberately fake authorization code is sent. Google checks the
    OAuth client first: a wrong Client ID or Client Secret is answered with ``invalid_client``,
    while a correct pair is answered with ``invalid_grant`` because the fake code is not real.
    No token is issued and nothing is changed in the Google account.
    """
    try:
        response = requests.post(
            _TOKEN_URI,
            data={
                "grant_type": "authorization_code",
                "code": _PROBE_CODE,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri or _PROBE_REDIRECT_URI,
            },
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.warning("Could not reach Google to check an OAuth client: %s", exc)
        return ClientCheck.UNREACHABLE

    if response.status_code == 429 or response.status_code >= 500:
        logger.warning("Google answered %s while checking an OAuth client", response.status_code)
        return ClientCheck.UNREACHABLE

    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    error = str(body.get("error", ""))
    description = str(body.get("error_description", "")).lower()

    if error in _CLIENT_ACCEPTED_ERRORS:
        return ClientCheck.VALID
    if error == "invalid_client":
        if "not found" in description:
            return ClientCheck.CLIENT_NOT_FOUND
        return ClientCheck.SECRET_MISMATCH
    if error == "unauthorized_client":
        return ClientCheck.REJECTED

    logger.warning(
        "Unexpected answer while checking an OAuth client: status=%s error=%s",
        response.status_code,
        error or "none",
    )
    return ClientCheck.UNKNOWN
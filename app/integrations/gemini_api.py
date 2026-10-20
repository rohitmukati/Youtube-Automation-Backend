# app/integrations/gemini_api.py
import logging

import requests

from app.config import settings

logger = logging.getLogger(__name__)

_BLOCK_FINISH_REASONS = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}


class GeminiError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


def _error_message(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message", ""))
    return ""


def generate_json_sync(*, model: str, system_prompt: str, user_prompt: str) -> str:
    if not settings.gemini_configured:
        raise GeminiError("not_configured", "The Gemini API key is not configured.")

    api_key = settings.gemini_api_key.get_secret_value().strip()
    url = f"{settings.gemini_api_base_url}/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": settings.gemini_temperature,
            "maxOutputTokens": settings.gemini_max_output_tokens,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=settings.gemini_timeout_seconds,
        )
    except requests.Timeout as exc:
        logger.warning("Gemini request timed out")
        raise GeminiError("unavailable", "The AI service took too long to answer.") from exc
    except requests.RequestException as exc:
        logger.warning("Gemini request failed: %s", exc.__class__.__name__)
        raise GeminiError("unavailable", "The AI service could not be reached.") from exc

    if response.status_code != 200:
        detail = _error_message(response)
        logger.warning("Gemini answered %s: %s", response.status_code, detail[:300])
        if response.status_code in (401, 403) or (
            response.status_code == 400 and "api key" in detail.lower()
        ):
            raise GeminiError("auth", "The AI service rejected the server API key.")
        if response.status_code == 429:
            raise GeminiError("rate_limited", "The AI service is busy.")
        if response.status_code == 404:
            raise GeminiError("unavailable", "The configured AI model is not available.")
        raise GeminiError("unavailable", "The AI service could not process the request.")

    try:
        data = response.json()
    except ValueError as exc:
        raise GeminiError("empty", "The AI service sent an unreadable answer.") from exc

    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiError("blocked", "The AI service blocked this request.")

    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError("empty", "The AI service returned no answer.")

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))

    if finish_reason == "MAX_TOKENS":
        raise GeminiError("truncated", "The AI answer was cut off.")
    if finish_reason in _BLOCK_FINISH_REASONS and not text.strip():
        raise GeminiError("blocked", "The AI service blocked this answer.")
    if not text.strip():
        raise GeminiError("empty", "The AI service returned an empty answer.")
    return text
import re

from app.config import settings

PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")
NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 100


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) < NAME_MIN_LENGTH:
        raise ValueError(f"Name must be at least {NAME_MIN_LENGTH} characters long.")
    if len(cleaned) > NAME_MAX_LENGTH:
        raise ValueError(f"Name must be at most {NAME_MAX_LENGTH} characters long.")
    return cleaned


def normalize_phone(value):
    if value is None:
        return None
    cleaned = re.sub(r"[\s\-()]", "", value)
    if not cleaned:
        return None
    if not PHONE_PATTERN.match(cleaned):
        raise ValueError("Phone number must contain 7 to 15 digits and may start with a plus sign.")
    return cleaned


def validate_password_strength(value: str) -> str:
    if len(value) < settings.password_min_length:
        raise ValueError(
            f"Password must be at least {settings.password_min_length} characters long."
        )
    if not re.search(r"[A-Za-z]", value) or not re.search(r"[0-9]", value):
        raise ValueError("Password must contain at least one letter and one number.")
    return value


def validate_otp_format(value: str) -> str:
    cleaned = value.strip()
    if not cleaned.isdigit() or len(cleaned) != settings.otp_length:
        raise ValueError(f"Verification code must be exactly {settings.otp_length} digits.")
    return cleaned

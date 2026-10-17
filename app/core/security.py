import asyncio
import hashlib
import hmac
import secrets
import string
import uuid
from datetime import timedelta
from typing import Any, Dict, Tuple

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings
from app.core.exceptions import InvalidTokenError, TokenExpiredError
from app.utils.datetime_utils import utc_now

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_BYTES = 48

_password_hasher = PasswordHasher()
_timing_reference_hash = _password_hasher.hash(secrets.token_urlsafe(16))


def _hash_password_sync(plain_password: str) -> str:
    return _password_hasher.hash(plain_password)


def _verify_password_sync(plain_password: str, hashed_password: str) -> bool:
    try:
        return _password_hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(plain_password: str) -> str:
    return await asyncio.to_thread(_hash_password_sync, plain_password)


async def verify_password(plain_password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(_verify_password_sync, plain_password, hashed_password)


async def burn_password_check(plain_password: str) -> None:
    await asyncio.to_thread(_verify_password_sync, plain_password, _timing_reference_hash)


def password_needs_rehash(hashed_password: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(hashed_password)
    except InvalidHashError:
        return False


def create_access_token(user_id: uuid.UUID) -> Tuple[str, int]:
    issued_at = utc_now()
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "type": ACCESS_TOKEN_TYPE,
        "jti": uuid.uuid4().hex,
        "iat": issued_at,
        "exp": issued_at + lifetime,
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError() from exc
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise InvalidTokenError()
    return payload


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_otp() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(settings.otp_length))


def hash_otp(email: str, otp: str) -> str:
    key = settings.jwt_secret_key.get_secret_value().encode("utf-8")
    message = f"{email}:{otp}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_otp(email: str, otp: str, hashed_otp: str) -> bool:
    return hmac.compare_digest(hash_otp(email, otp), hashed_otp)

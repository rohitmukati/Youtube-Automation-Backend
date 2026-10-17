"""
Fernet symmetric encryption for OAuth tokens.
TOKEN_ENCRYPTION_KEY .env mein hona chahiye.

Key generate karne ka command:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _get_fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY .env mein set nahi hai. "
            "Tokens encrypt nahi ho sakte."
        )
    key = settings.token_encryption_key.get_secret_value()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plain_token: str) -> str:
    """Token ko encrypt karke string return karta hai (DB mein store hoga)."""
    fernet = _get_fernet()
    return fernet.encrypt(plain_token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token: str) -> str:
    """DB se liya encrypted token decrypt karke plain token return karta hai."""
    try:
        fernet = _get_fernet()
        return fernet.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Token decrypt nahi ho saka — key galat ho sakti hai.") from exc
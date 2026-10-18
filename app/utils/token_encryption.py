"""
Fernet symmetric encryption for OAuth tokens.
TOKEN_ENCRYPTION_KEY must be present in the .env file.

Command to generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _get_fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set in the .env file. "
            "Tokens cannot be encrypted."
        )
    key = settings.token_encryption_key.get_secret_value()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plain_token: str) -> str:
    """Encrypts a token and returns it as a string for storage in the database."""
    fernet = _get_fernet()
    return fernet.encrypt(plain_token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token: str) -> str:
    """Decrypts an encrypted token from the database and returns the plain token."""
    try:
        fernet = _get_fernet()
        return fernet.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Token decryption failed — the key may be invalid.") from exc
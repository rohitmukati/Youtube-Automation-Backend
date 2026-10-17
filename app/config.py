from functools import lru_cache
from pathlib import Path
from typing import Annotated, List, Optional

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Youtube Automation Backend"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    frontend_url: str = "http://localhost:3000"
    cors_origins: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_auto_create_tables: bool = True

    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=30, ge=1)
    refresh_token_expire_days: int = Field(default=7, ge=1)

    password_min_length: int = Field(default=8, ge=6, le=64)
    otp_length: int = Field(default=6, ge=4, le=8)
    password_reset_otp_expire_minutes: int = Field(default=10, ge=1)
    password_reset_otp_max_attempts: int = Field(default=5, ge=1)
    password_reset_otp_resend_cooldown_seconds: int = Field(default=60, ge=0)

    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[SecretStr] = None
    supabase_storage_bucket: str = "artifacts"

    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[SecretStr] = None
    smtp_from_email: Optional[str] = None
    smtp_from_name: str = "Youtube Automation"
    smtp_use_tls: bool = True
    smtp_timeout_seconds: int = Field(default=15, ge=1)

    google_client_id: Optional[str] = None
    google_client_secret: Optional[SecretStr] = None
    google_redirect_uri: Optional[str] = None
    token_encryption_key: Optional[SecretStr] = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value):
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if raw.startswith("postgres://"):
            raw = "postgresql+asyncpg://" + raw[len("postgres://"):]
        elif raw.startswith("postgresql://"):
            raw = "postgresql+asyncpg://" + raw[len("postgresql://"):]
        allowed_prefixes = ("postgresql+asyncpg://", "sqlite+aiosqlite://")
        if not raw.startswith(allowed_prefixes):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL connection string, "
                "for example postgresql://user:password@host:5432/postgres"
            )
        return SecretStr(raw)

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters long")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return level

    @field_validator("smtp_password")
    @classmethod
    def strip_smtp_password_spaces(cls, value: Optional[SecretStr]) -> Optional[SecretStr]:
        if value is None:
            return value
        return SecretStr(value.get_secret_value().replace(" ", ""))

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def smtp_configured(self) -> bool:
        return all(
            [
                self.smtp_host,
                self.smtp_username,
                self.smtp_password and self.smtp_password.get_secret_value(),
                self.smtp_from_email,
            ]
        )


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
            problems.append(f"{location}: {error['msg']}")
        raise RuntimeError(
            "Environment configuration is invalid or incomplete. "
            "Please fix your .env file and restart the application. "
            "Details: " + " | ".join(problems)
        ) from exc


settings = get_settings()

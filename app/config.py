from functools import lru_cache
from pathlib import Path
from typing import Annotated, ClassVar, List, Optional

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

    app_name: ClassVar[str] = "Youtube Automation Backend"
    debug: ClassVar[bool] = False
    log_level: ClassVar[str] = "INFO"
    api_v1_prefix: ClassVar[str] = "/api/v1"

    db_pool_size: ClassVar[int] = 5
    db_max_overflow: ClassVar[int] = 10
    db_auto_create_tables: ClassVar[bool] = True

    jwt_algorithm: ClassVar[str] = "HS256"
    access_token_expire_minutes: ClassVar[int] = 30
    refresh_token_expire_days: ClassVar[int] = 7

    password_min_length: ClassVar[int] = 8
    otp_length: ClassVar[int] = 6
    password_reset_otp_expire_minutes: ClassVar[int] = 10
    password_reset_otp_max_attempts: ClassVar[int] = 5
    password_reset_otp_resend_cooldown_seconds: ClassVar[int] = 60

    smtp_host: ClassVar[str] = "smtp.gmail.com"
    smtp_port: ClassVar[int] = 587
    smtp_use_tls: ClassVar[bool] = True
    smtp_timeout_seconds: ClassVar[int] = 15
    smtp_from_name: ClassVar[str] = "Youtube Automation"

    supabase_storage_bucket: ClassVar[str] = "artifacts"

    app_env: str = "development"
    cors_origins: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )

    database_url: SecretStr
    jwt_secret_key: SecretStr

    smtp_username: Optional[str] = None
    smtp_password: Optional[SecretStr] = None

    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[SecretStr] = None

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
    def smtp_from_email(self) -> Optional[str]:
        return self.smtp_username

    @property
    def smtp_configured(self) -> bool:
        return bool(
            self.smtp_username
            and self.smtp_password
            and self.smtp_password.get_secret_value()
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

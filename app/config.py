# app/config.py
import json
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
    app_env: ClassVar[str] = "development"
    debug: ClassVar[bool] = True
    log_level: ClassVar[str] = "INFO"
    log_sql: ClassVar[bool] = False
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

    storage_signed_url_ttl_seconds: ClassVar[int] = 3600
    storage_request_timeout_seconds: ClassVar[int] = 30
    storage_upload_timeout_seconds: ClassVar[int] = 600
    storage_video_max_bytes: ClassVar[int] = 50 * 1024 * 1024
    storage_image_max_bytes: ClassVar[int] = 10 * 1024 * 1024
    storage_video_content_types: ClassVar[List[str]] = [
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
        "video/x-matroska",
        "video/webm",
        "video/mpeg",
    ]
    storage_image_content_types: ClassVar[List[str]] = [
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    ]
    storage_max_tags: ClassVar[int] = 20
    storage_max_tag_length: ClassVar[int] = 30
    storage_list_default_limit: ClassVar[int] = 20
    storage_list_max_limit: ClassVar[int] = 100

    yt_upload_max_accounts: ClassVar[int] = 50
    yt_upload_max_attempts: ClassVar[int] = 3
    yt_upload_retry_delays_seconds: ClassVar[List[int]] = [30, 120]
    yt_upload_chunk_bytes: ClassVar[int] = 8 * 1024 * 1024
    yt_upload_chunk_retries: ClassVar[int] = 3
    yt_upload_concurrency: ClassVar[int] = 3
    yt_upload_daily_limit: ClassVar[int] = 100
    yt_upload_quota_timezone: ClassVar[str] = "America/Los_Angeles"
    yt_upload_worker_enabled: ClassVar[bool] = True
    yt_upload_worker_poll_seconds: ClassVar[int] = 3
    yt_upload_worker_shutdown_seconds: ClassVar[int] = 60
    yt_upload_stale_after_seconds: ClassVar[int] = 600
    yt_upload_stale_sweep_seconds: ClassVar[int] = 60
    yt_upload_source_url_ttl_seconds: ClassVar[int] = 6 * 3600
    yt_upload_progress_interval_seconds: ClassVar[int] = 3
    yt_upload_range_timeout_seconds: ClassVar[int] = 60
    yt_upload_error_max_length: ClassVar[int] = 500
    yt_title_max_length: ClassVar[int] = 100
    yt_description_max_bytes: ClassVar[int] = 5000
    yt_tag_max_length: ClassVar[int] = 30
    yt_max_tags: ClassVar[int] = 100
    yt_tags_max_total_chars: ClassVar[int] = 500
    yt_default_category_id: ClassVar[str] = "22"
    yt_default_privacy: ClassVar[str] = "private"
    yt_thumbnail_content_types: ClassVar[List[str]] = ["image/jpeg", "image/png"]
    yt_watch_url_template: ClassVar[str] = "https://www.youtube.com/watch?v={video_id}"
    yt_list_default_limit: ClassVar[int] = 20
    yt_list_max_limit: ClassVar[int] = 100
    yt_account_notes_max_length: ClassVar[int] = 500
    google_userinfo_url: ClassVar[str] = "https://www.googleapis.com/oauth2/v3/userinfo"
    google_userinfo_timeout_seconds: ClassVar[int] = 10
    google_client_id_pattern: ClassVar[str] = r"^[A-Za-z0-9._-]{8,200}\.apps\.googleusercontent\.com$"
    google_client_secret_min_length: ClassVar[int] = 8
    google_client_secret_max_length: ClassVar[int] = 256
    google_redirect_uri: ClassVar[str] = "http://localhost:8000/api/v1/youtube/callback"
    google_scopes: ClassVar[List[str]] = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    ]

    # ── Loaded from .env / environment variables ──────────────
    database_url: SecretStr
    jwt_secret_key: SecretStr

    # FRONTEND_URL=https://your-frontend.com
    frontend_url: str = "http://localhost:3000"

    # CORS_ORIGINS=http://localhost:3000,http://localhost:5173
    # (comma-separated; JSON list '["a","b"]' bhi chalega)
    cors_origins: Annotated[List[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    smtp_username: Optional[str] = None
    smtp_password: Optional[SecretStr] = None

    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[SecretStr] = None

    token_encryption_key: Optional[SecretStr] = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError("CORS_ORIGINS looks like JSON but is invalid") from exc
                return [str(item).strip().rstrip("/") for item in parsed if str(item).strip()]
            return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip().rstrip("/") for item in value if str(item).strip()]
        return value

    @field_validator("frontend_url")
    @classmethod
    def normalize_frontend_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

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
                "e.g. postgresql://user:password@host:5432/postgres"
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
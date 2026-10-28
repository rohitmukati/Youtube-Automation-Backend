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

    storage_region: ClassVar[str] = "auto"
    storage_signed_url_ttl_seconds: ClassVar[int] = 3600
    storage_signed_url_max_ttl_seconds: ClassVar[int] = 7 * 24 * 3600
    storage_request_timeout_seconds: ClassVar[int] = 30
    storage_upload_timeout_seconds: ClassVar[int] = 600
    storage_connect_timeout_seconds: ClassVar[int] = 10
    storage_max_attempts: ClassVar[int] = 3
    storage_max_pool_connections: ClassVar[int] = 64
    storage_multipart_chunk_bytes: ClassVar[int] = 8 * 1024 * 1024
    storage_multipart_concurrency: ClassVar[int] = 4
    storage_multipart_buffered_chunks: ClassVar[int] = 6
    storage_image_max_bytes: ClassVar[int] = 10 * 1024 * 1024
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

    media_video_max_bytes: ClassVar[int] = int(1.5 * 1024 * 1024 * 1024)
    media_video_content_types: ClassVar[List[str]] = [
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
        "video/x-matroska",
        "video/webm",
        "video/mpeg",
    ]
    media_video_extensions: ClassVar[List[str]] = [
        ".mp4",
        ".m4v",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".mpeg",
        ".mpg",
    ]
    media_staging_prefix: ClassVar[str] = "staging"
    media_part_size_bytes: ClassVar[int] = 16 * 1024 * 1024
    media_min_part_size_bytes: ClassVar[int] = 5 * 1024 * 1024
    media_max_parts: ClassVar[int] = 10000
    media_part_url_ttl_seconds: ClassVar[int] = 6 * 3600
    media_client_parallel_parts: ClassVar[int] = 4
    media_retention_hours: ClassVar[int] = 24
    media_upload_window_hours: ClassVar[int] = 12
    media_max_active_per_user: ClassVar[int] = 5
    media_max_staged_bytes_total: ClassVar[int] = 8 * 1024 * 1024 * 1024
    media_reaper_interval_seconds: ClassVar[int] = 300
    media_reaper_batch_size: ClassVar[int] = 50
    media_signature_probe_bytes: ClassVar[int] = 32
    media_file_name_max_length: ClassVar[int] = 255

    yt_upload_max_accounts: ClassVar[int] = 50
    yt_upload_max_attempts: ClassVar[int] = 6
    yt_upload_retry_delays_seconds: ClassVar[List[int]] = [20, 60, 180, 300, 600]
    yt_upload_api_url: ClassVar[str] = "https://www.googleapis.com/upload/youtube/v3/videos"
    yt_upload_chunk_bytes: ClassVar[int] = 16 * 1024 * 1024
    yt_upload_chunk_alignment_bytes: ClassVar[int] = 256 * 1024
    yt_upload_chunk_retries: ClassVar[int] = 5
    yt_upload_chunk_backoff_seconds: ClassVar[float] = 2.0
    yt_upload_chunk_backoff_max_seconds: ClassVar[float] = 30.0
    yt_upload_http_timeout_seconds: ClassVar[int] = 300
    yt_upload_concurrency: ClassVar[int] = 10
    yt_upload_daily_limit: ClassVar[int] = 100
    yt_upload_quota_timezone: ClassVar[str] = "America/Los_Angeles"
    yt_upload_worker_enabled: ClassVar[bool] = True
    yt_upload_worker_poll_seconds: ClassVar[int] = 3
    yt_upload_worker_shutdown_seconds: ClassVar[int] = 60
    yt_upload_heartbeat_seconds: ClassVar[int] = 15
    yt_upload_progress_interval_seconds: ClassVar[int] = 3
    yt_upload_stale_after_seconds: ClassVar[int] = 120
    yt_upload_stale_sweep_seconds: ClassVar[int] = 30
    yt_upload_source_url_ttl_seconds: ClassVar[int] = 12 * 3600
    yt_upload_range_timeout_seconds: ClassVar[int] = 60
    yt_upload_range_retries: ClassVar[int] = 4
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
    google_scopes: ClassVar[List[str]] = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]
    google_required_scopes: ClassVar[List[str]] = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]
    yt_analytics_scope: ClassVar[str] = "https://www.googleapis.com/auth/yt-analytics.readonly"
    yt_analytics_max_videos: ClassVar[int] = 2000
    yt_analytics_page_size: ClassVar[int] = 50
    yt_analytics_sync_ttl_seconds: ClassVar[int] = 6 * 3600
    yt_analytics_sync_stale_seconds: ClassVar[int] = 900
    yt_analytics_report_ttl_seconds: ClassVar[int] = 3 * 3600
    yt_analytics_comments_ttl_seconds: ClassVar[int] = 6 * 3600
    yt_analytics_data_delay_days: ClassVar[int] = 2
    yt_analytics_default_days: ClassVar[int] = 28
    yt_analytics_max_days: ClassVar[int] = 730
    yt_analytics_top_videos_default: ClassVar[int] = 25
    yt_analytics_top_videos_max: ClassVar[int] = 200
    yt_analytics_search_terms_limit: ClassVar[int] = 25
    yt_analytics_country_limit: ClassVar[int] = 25
    yt_analytics_comments_limit: ClassVar[int] = 100
    yt_analytics_min_video_age_days: ClassVar[int] = 7
    yt_analytics_min_videos_for_insights: ClassVar[int] = 10
    yt_analytics_min_slot_videos: ClassVar[int] = 2
    yt_analytics_min_tag_videos: ClassVar[int] = 2
    yt_analytics_min_group_videos: ClassVar[int] = 3
    yt_analytics_neighbor_window_days: ClassVar[int] = 90
    yt_analytics_neighbor_min: ClassVar[int] = 3
    yt_analytics_default_timezone: ClassVar[str] = "Asia/Kolkata"
    yt_analytics_short_max_seconds: ClassVar[int] = 180
    yt_analytics_sync_error_max_length: ClassVar[int] = 300
    yt_analytics_snapshot_retention_days: ClassVar[int] = 800
    gemini_api_base_url: ClassVar[str] = "https://generativelanguage.googleapis.com/v1beta"
    gemini_timeout_seconds: ClassVar[int] = 120
    gemini_temperature: ClassVar[float] = 0.4
    gemini_max_output_tokens: ClassVar[int] = 16384
    gemini_max_attempts: ClassVar[int] = 2
    yt_ai_report_cache_hours: ClassVar[int] = 12
    yt_ai_force_cooldown_seconds: ClassVar[int] = 120
    yt_ai_daily_report_limit: ClassVar[int] = 30
    yt_ai_default_days: ClassVar[int] = 28
    yt_ai_comment_videos: ClassVar[int] = 5
    yt_ai_comments_per_video: ClassVar[int] = 40
    yt_ai_description_max_chars: ClassVar[int] = 1500
    yt_ai_list_default_limit: ClassVar[int] = 20

    # ── Loaded from .env / environment variables ──────────────
    database_url: SecretStr
    jwt_secret_key: SecretStr

    # GOOGLE_REDIRECT_URI=https://your-backend.com/api/v1/youtube/callback
    google_redirect_uri: str = "http://localhost:8000/api/v1/youtube/callback"

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

    # APP_ENV=production turns off the interactive docs and debug behaviour
    app_env: str = "development"

    # Cloudflare R2 (S3 compatible). R2_ENDPOINT_URL is optional when R2_ACCOUNT_ID is set.
    r2_account_id: Optional[str] = None
    r2_endpoint_url: Optional[str] = None
    r2_bucket_name: Optional[str] = None
    r2_access_key_id: Optional[str] = None
    r2_secret_access_key: Optional[SecretStr] = None

    token_encryption_key: Optional[SecretStr] = None

    # GEMINI_API_KEY=your-google-ai-studio-key
    gemini_api_key: Optional[SecretStr] = None

    # GEMINI_MODEL_FAST / GEMINI_MODEL_DEEP can be changed without touching the code
    gemini_model_fast: str = "gemini-3.5-flash"
    gemini_model_deep: str = "gemini-3.5-flash"

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
        return self.app_env.strip().lower() == "production"

    @property
    def debug(self) -> bool:
        return not self.is_production

    @property
    def r2_endpoint(self) -> Optional[str]:
        explicit = (self.r2_endpoint_url or "").strip().rstrip("/")
        if explicit:
            return explicit
        account = (self.r2_account_id or "").strip()
        return f"https://{account}.r2.cloudflarestorage.com" if account else None

    @property
    def r2_configured(self) -> bool:
        return bool(
            self.r2_endpoint
            and (self.r2_bucket_name or "").strip()
            and (self.r2_access_key_id or "").strip()
            and self.r2_secret_access_key
            and self.r2_secret_access_key.get_secret_value().strip()
        )

    @property
    def media_part_count_limit(self) -> int:
        return self.media_max_parts

    @property
    def smtp_from_email(self) -> Optional[str]:
        return self.smtp_username

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key and self.gemini_api_key.get_secret_value().strip())

    @property
    def smtp_configured(self) -> bool:
        return bool(
            self.smtp_username
            and self.smtp_password
            and self.smtp_password.get_secret_value()
        )

def _check_fixed_values() -> None:
    problems = []
    if Settings.yt_upload_chunk_bytes % Settings.yt_upload_chunk_alignment_bytes:
        problems.append("yt_upload_chunk_bytes must be a multiple of 256 KiB")
    if Settings.media_part_size_bytes < Settings.media_min_part_size_bytes:
        problems.append("media_part_size_bytes must be at least media_min_part_size_bytes")
    if Settings.media_video_max_bytes > Settings.media_part_size_bytes * Settings.media_max_parts:
        problems.append("media_video_max_bytes does not fit in media_max_parts parts")
    if len(Settings.yt_upload_retry_delays_seconds) < 1:
        problems.append("yt_upload_retry_delays_seconds cannot be empty")
    if problems:
        raise RuntimeError("Invalid fixed settings: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    _check_fixed_values()
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
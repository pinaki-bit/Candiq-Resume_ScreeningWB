"""
backend/app/config.py

Application settings loaded from environment variables / .env file.
Uses Pydantic v2 BaseSettings so every value is type-validated at startup.

Security enhancements (v0.3.0):
  - Auto-generates a cryptographic SECRET_KEY if the placeholder is detected.
  - Refuses to start in production with default credentials.
  - API docs are disabled in production environments.

Adding a new setting:
  1. Add the field here with a sensible default.
  2. Add it to .env.example with a comment.
  3. Never hardcode secrets — use environment variables or a secret manager.
"""

from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Project root — three levels up from this file: app/ → backend/ → root
_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# The sentinel value used to detect unchanged default secret keys.
_DEFAULT_SECRET_SENTINEL = "CHANGE_ME_USE_openssl_rand_hex_32"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        protected_namespaces=(),   # allow field names starting with "model_"
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_env: str = "development"
    app_title: str = "Candiq API"
    app_version: str = "0.3.0"

    # ------------------------------------------------------------------
    # Security / JWT
    # ------------------------------------------------------------------
    secret_key: str = _DEFAULT_SECRET_SENTINEL
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str = "sqlite:///./resume_screening.db"

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    allowed_origins: str = "http://localhost:8501,http://127.0.0.1:8501,http://localhost:5173,http://127.0.0.1:5173"

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _keep_origins_string(cls, v: str) -> str:
        return v

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------
    upload_dir: str = str(_BACKEND_ROOT / "uploads")
    max_upload_size_mb: int = 10          # megabytes
    allowed_extensions: str = "pdf"       # comma-separated

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def allowed_extension_set(self) -> set[str]:
        return {ext.strip().lower() for ext in self.allowed_extensions.split(",")}

    # ------------------------------------------------------------------
    # AWS S3 / Cloud Storage
    # ------------------------------------------------------------------
    use_s3_storage: bool = False
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str | None = "us-east-1"
    aws_s3_bucket: str | None = None
    s3_endpoint_url: str | None = None  # Useful for Cloudflare R2 / Supabase / MinIO

    # ------------------------------------------------------------------
    # ML / model artifacts & OOD Policy
    # ------------------------------------------------------------------
    model_dir: str = str(_BACKEND_ROOT.parent / "ml" / "artifacts")
    active_model_filename: str = "model_latest.joblib"
    ood_enabled: bool = True
    ood_confidence_threshold: float = 0.85  # Frozen Phase 24 OP-4 threshold (91.01% test coverage, 96.85% OOD dev rejection)
    ood_policy_version: str = "v1.0-phase24-op4"

    @property
    def active_model_path(self) -> str:
        return os.path.join(self.model_dir, self.active_model_filename)

    # ------------------------------------------------------------------
    # Embeddings / Semantic Search
    # ------------------------------------------------------------------
    embedding_provider: str = "sentence_transformer"  # "sentence_transformer" | "local" | "openai" | "bge"
    sentence_transformer_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_device: str = "cpu"
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    # ------------------------------------------------------------------
    # Generative AI / LLM Architecture
    # ------------------------------------------------------------------
    ai_provider: str = "mock"             # "mock" | "openai" | "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    openai_model: str = "gpt-4o-mini"

    # ------------------------------------------------------------------
    # NLP
    # ------------------------------------------------------------------
    spacy_model: str = "en_core_web_sm"

    # Path to shared skill taxonomy (relative to backend root)
    skill_taxonomy_path: str = str(
        _BACKEND_ROOT.parent / "shared" / "skill_taxonomy.json"
    )
    label_mapping_path: str = str(
        _BACKEND_ROOT.parent / "shared" / "label_mapping.json"
    )

    # ------------------------------------------------------------------
    # Email / SMTP
    # ------------------------------------------------------------------
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = "noreply@example.com"
    email_enabled: bool = False           # Set True in production

    # ------------------------------------------------------------------
    # Rate limiting (requests per window)
    # ------------------------------------------------------------------
    rate_limit_login: str = "10/minute"
    rate_limit_upload: str = "20/minute"
    rate_limit_default: str = "200/minute"

    # ------------------------------------------------------------------
    # Seed admin (dev only — override in production)
    # ------------------------------------------------------------------
    admin_email: str = "admin@example.com"
    admin_password: str = "changeme123"

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Derived security properties
    # ------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in ("production", "prod", "staging")

    @property
    def has_default_secret(self) -> bool:
        return self.secret_key == _DEFAULT_SECRET_SENTINEL

    @property
    def has_default_admin_password(self) -> bool:
        return self.admin_password == "changeme123"

    @property
    def docs_url(self) -> str | None:
        """Disable Swagger UI in production."""
        return None if self.is_production else "/docs"

    @property
    def redoc_url(self) -> str | None:
        """Disable ReDoc in production."""
        return None if self.is_production else "/redoc"

    @property
    def openapi_url(self) -> str | None:
        """Disable OpenAPI schema in production."""
        return None if self.is_production else "/openapi.json"


def _auto_generate_secret_key(settings: Settings) -> Settings:
    """
    If the SECRET_KEY is the default placeholder, generate a cryptographically
    secure key and persist it to the .env file so it survives restarts.

    This prevents the catastrophic scenario of running with a known key.
    """
    if not settings.has_default_secret:
        return settings

    new_key = secrets.token_hex(32)

    # Try to update .env file
    env_path = _BACKEND_ROOT / ".env"
    try:
        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
            if _DEFAULT_SECRET_SENTINEL in content:
                content = content.replace(_DEFAULT_SECRET_SENTINEL, new_key)
                env_path.write_text(content, encoding="utf-8")
                logger.warning(
                    "🔐 AUTO-GENERATED a new SECRET_KEY and saved to .env. "
                    "The default placeholder has been replaced."
                )
            else:
                # SECRET_KEY line not found — append it
                with env_path.open("a", encoding="utf-8") as f:
                    f.write(f"\nSECRET_KEY={new_key}\n")
                logger.warning("🔐 AUTO-GENERATED a new SECRET_KEY and appended to .env.")
        else:
            # No .env — create one with the key
            env_path.write_text(
                f"# Auto-generated — do NOT commit to version control\n"
                f"SECRET_KEY={new_key}\n",
                encoding="utf-8",
            )
            logger.warning("🔐 Created .env with a new SECRET_KEY.")
    except OSError as exc:
        logger.error(
            "⚠️  Could not write SECRET_KEY to .env (%s). "
            "Using generated key for this session only — it will NOT persist!",
            exc,
        )

    # Update the settings object in memory
    settings.secret_key = new_key
    return settings


def startup_security_checks(settings: Settings) -> None:
    """
    Run security validations at startup. Logs warnings in development,
    refuses to start in production with insecure configuration.
    """
    issues: list[str] = []

    if settings.has_default_secret:
        issues.append(
            "SECRET_KEY is the default placeholder. "
            "Generate one with: openssl rand -hex 32"
        )

    if settings.has_default_admin_password:
        if settings.is_production:
            issues.append(
                "ADMIN_PASSWORD is the default 'changeme123'. "
                "Set a strong password via ADMIN_PASSWORD env var."
            )
        else:
            logger.warning(
                "⚠️  ADMIN_PASSWORD is the default 'changeme123'. "
                "The admin will be required to change it on first login."
            )

    if settings.is_production and issues:
        for issue in issues:
            logger.critical("🚫 SECURITY: %s", issue)
        raise RuntimeError(
            "Cannot start in production with insecure configuration. "
            f"Fix {len(issues)} issue(s) listed above."
        )
    elif issues:
        for issue in issues:
            logger.warning("⚠️  SECURITY: %s", issue)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (loaded once per process)."""
    settings = Settings()
    settings = _auto_generate_secret_key(settings)
    return settings

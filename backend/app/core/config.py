import os
from pathlib import Path
from functools import lru_cache
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"
ENV_TXT_FILE = BASE_DIR / ".env.txt"

# --- 1. AUTO-FIX HIDDEN WINDOWS EXTENSIONS ---
if ENV_TXT_FILE.exists() and not ENV_FILE.exists():
    print("\n⚠️ WARNING: Found '.env.txt'. Windows hid the extension! Renaming to '.env' automatically...\n")
    ENV_TXT_FILE.rename(ENV_FILE)

# --- 2. LOAD .env IF PRESENT (silently skip in production where env vars are injected) ---
if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE, override=False)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- CONFIG FIELDS ---
    app_env: Literal["dev", "staging", "prod"] = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    
    api_key: str = Field(min_length=8)
    database_url: str
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_echo: bool = False

    storage_backend: Literal["local", "s3"] = "local"
    data_dir: Path = BASE_DIR / "data"
    max_upload_bytes: int = 100 * 1024 * 1024

    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o"
    openai_mini_model: str = "gpt-4o-mini"
    openai_embed_model: str = "text-embedding-3-small"

    # --- health grades ---
    health_grade_a: int = 90
    health_grade_b: int = 75
    health_grade_c: int = 60
    health_grade_d: int = 45

    # --- JWT ---
    jwt_secret_key: str = Field(default="change-me-in-production", min_length=16)
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # --- rate limits (slowapi expressions, e.g. "10/hour") ---
    rate_limit_upload: str = "10/hour"
    rate_limit_chat_message: str = "60/hour"
    rate_limit_interpret: str = "20/hour"
    rate_limit_session_create: str = "20/hour"
    rate_limit_global: str = "200/hour"
    max_concurrent_pipelines: int = 3

    # --- upload abuse protection ---
    max_upload_rows: int = 5_000_000

    # --- request size + security ---
    max_request_bytes: int = 110 * 1024 * 1024

    # --- frontend ---
    frontend_url: str = "http://localhost:5173"

    # --- email (SMTP) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@neuronova.ai"
    smtp_tls: bool = True

    # --- Google OAuth ---
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"

    # --- DIRECTORIES ---
    @property
    def raw_dir(self) -> Path: return self.data_dir / "raw"
    @property
    def profiles_dir(self) -> Path: return self.data_dir / "profiles"
    @property
    def findings_dir(self) -> Path: return self.data_dir / "findings"
    @property
    def viz_dir(self) -> Path: return self.data_dir / "viz"
    @property
    def llm_cache_dir(self) -> Path: return self.data_dir / "llm_cache"
    @property
    def pipeline_dir(self) -> Path: return self.data_dir / "pipeline"

    def ensure_data_dirs(self) -> None:
        for d in (self.raw_dir, self.profiles_dir, self.findings_dir, 
                  self.viz_dir, self.llm_cache_dir, self.pipeline_dir):
            d.mkdir(parents=True, exist_ok=True)

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
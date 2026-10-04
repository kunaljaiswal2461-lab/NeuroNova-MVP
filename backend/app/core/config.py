import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from functools import lru_cache
from typing import Literal
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"
ENV_TXT_FILE = BASE_DIR / ".env.txt"

# The direct-query AST validator imports this registry to enforce the
# positional + keyword argument count of every module-level ``pl.*`` call it
# permits.  ``None`` as the maximum means "one or more" or "zero or more",
# depending on the minimum.  Keep this explicit rather than introspecting
# Polars at runtime: the registry is part of our supported query language and
# can be reviewed alongside a Polars upgrade.
POLARS_FUNCTION_SIGNATURES: dict[str, tuple[int, int | None]] = {
    "col": (1, 1),
    "lit": (1, 1),
    "when": (1, 1),
    "sum": (1, None),
    "mean": (1, None),
    "median": (1, None),
    "min": (1, None),
    "max": (1, None),
    "len": (0, 0),
    "count": (0, 0),
    "n_unique": (1, None),
    "std": (1, None),
    "var": (1, None),
    "quantile": (1, None),
    "corr": (2, 2),
    "cov": (2, 2),
    "first": (1, None),
    "last": (1, None),
    "nth": (1, 1),
    "all": (0, None),
    "any": (0, None),
    "exclude": (1, None),
    "concat_str": (1, None),
    "concat_list": (1, None),
    "concat": (1, 2),
    "sum_horizontal": (1, None),
    "min_horizontal": (1, None),
    "max_horizontal": (1, None),
    "mean_horizontal": (1, None),
    "coalesce": (1, None),
    "struct": (1, None),
    "format": (1, None),
    "date": (3, 3),
    "datetime": (3, 8),
    "duration": (0, 9),
    "int_range": (2, 3),
    "date_range": (2, 3),
}

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
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)

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
    rate_limit_connector_test: str = "10/hour"
    rate_limit_connector_create: str = "10/hour"
    rate_limit_catalog_sync_request: str = "20/hour"
    # Profile work is an explicit, potentially expensive owner action. The
    # route limiter complements durable worker concurrency; neither starts a
    # profile automatically.
    rate_limit_source_profile_request: str = "10/hour"
    rate_limit_global: str = "200/hour"
    max_concurrent_pipelines: int = Field(default=1, gt=0)

    # --- upload abuse protection ---
    max_upload_rows: int = 5_000_000

    # --- request size + security ---
    # One MiB of headroom for multipart headers around the pilot file limit.
    max_request_bytes: int = Field(default=21 * 1024 * 1024, gt=0)

    # --- external PostgreSQL sources (disabled until production egress is ready) ---
    # These are intentionally optional: file-only deployments must start without
    # connector secrets. The feature service fails closed when they are absent.
    connectors_enabled: bool = False
    connector_egress_policy_configured: bool = False
    connector_credentials_active_key_id: str = Field(default="", max_length=64)
    connector_credentials_active_key: SecretStr | None = None
    # JSON object of at most three historical {key_id: fernet_key} entries.
    # It is secret material and must be injected by the deployment, never
    # committed to an .env example.
    connector_credentials_previous_keyring: SecretStr | None = None
    connector_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    connector_statement_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    connector_operation_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    connector_max_concurrent_operations: int = Field(default=2, ge=1, le=10)
    connector_max_inventory_tables: int = Field(default=1000, ge=1, le=1000)

    # --- durable PostgreSQL catalog discovery worker ---
    # The web process only persists a manual request. A separately deployed
    # worker consumes it, so file-only web deployments remain unaffected.
    catalog_worker_max_concurrent_jobs: int = Field(default=1, ge=1, le=10)
    catalog_worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=30)
    catalog_worker_lease_seconds: float = Field(default=30.0, ge=5, le=300)
    catalog_worker_lease_renewal_seconds: float = Field(default=5.0, ge=1, le=60)
    catalog_worker_max_attempts: int = Field(default=3, ge=1, le=10)
    catalog_worker_retry_base_seconds: float = Field(default=2.0, gt=0, le=60)
    catalog_worker_retry_max_seconds: float = Field(default=30.0, gt=0, le=300)
    catalog_discovery_timeout_seconds: float = Field(default=45.0, gt=0, le=120)
    catalog_discovery_max_tables: int = Field(default=1000, ge=1, le=1000)
    catalog_discovery_max_columns: int = Field(default=50_000, ge=1, le=100_000)
    catalog_discovery_max_relationships: int = Field(default=20_000, ge=1, le=50_000)
    catalog_discovery_max_metadata_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)

    # --- PostgreSQL structured-query planning (Stage 4A) ---
    # These caps constrain the small, deterministic SQL language before any
    # future connector can see a plan. They deliberately remain independent
    # from upload/Polars query limits.
    postgres_query_max_result_limit: int = Field(default=500, ge=1, le=10_000)
    postgres_query_max_projections: int = Field(default=20, ge=1, le=100)
    postgres_query_max_filters: int = Field(default=12, ge=0, le=50)
    postgres_query_max_aggregations: int = Field(default=8, ge=1, le=25)
    postgres_query_max_group_by: int = Field(default=4, ge=0, le=12)
    postgres_query_max_in_values: int = Field(default=100, ge=1, le=1000)
    # A conservative pre-asyncpg estimate for all bound plan values. Result
    # bytes remain a separate Stage 4B execution limit.
    postgres_query_max_parameter_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)

    # --- PostgreSQL controlled execution (Stage 4B; internal only) ---
    # These are deliberately independent from onboarding/discovery budgets.
    # A query has its own short-lived client connection and never borrows the
    # application database pool connection for remote work.
    postgres_execution_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    postgres_execution_statement_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    postgres_execution_lock_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    postgres_execution_idle_transaction_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    postgres_execution_operation_timeout_seconds: float = Field(default=12.0, gt=0, le=60)
    postgres_execution_max_concurrent_operations: int = Field(default=2, ge=1, le=20)
    postgres_execution_max_concurrent_per_user: int = Field(default=1, ge=1, le=5)
    postgres_execution_max_concurrent_per_source: int = Field(default=1, ge=1, le=5)
    # The compiler's requested LIMIT remains part of answer semantics. These
    # independent caps bound only materialization/presentation of that already
    # bounded result and are never injected below an aggregate.
    postgres_execution_max_result_rows: int = Field(default=500, ge=1, le=10_000)
    postgres_execution_max_result_bytes: int = Field(
        default=1 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024
    )
    postgres_execution_cursor_prefetch: int = Field(default=50, ge=1, le=1_000)

    # --- optional bounded PostgreSQL profiling (Stage 6A; disabled per source) ---
    # These are intentionally separate from analytical query budgets. A
    # profile uses fixed internal aggregate operations over a bounded
    # TABLESAMPLE population; it never accepts user/LLM SQL or falls back to a
    # full-table scan.
    postgres_profile_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    postgres_profile_statement_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    postgres_profile_lock_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    postgres_profile_idle_transaction_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    postgres_profile_operation_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    # SYSTEM sampling chooses blocks, not independent rows. The bounded LIMIT
    # caps returned sample population but does not claim to cap scan cost;
    # deadlines, fixed statement shape, and selection caps do that as well.
    # The transport has an independent hard cap below 100%. A deployment
    # setting must never turn the safe sampling path into a full-table read.
    postgres_profile_sample_percent: float = Field(default=1.0, gt=0, le=10)
    postgres_profile_sample_limit: int = Field(default=5_000, ge=1, le=10_000)
    postgres_profile_max_tables_per_run: int = Field(default=10, ge=1, le=100)
    postgres_profile_max_columns_per_table: int = Field(default=25, ge=0, le=50)
    postgres_profile_max_columns_per_run: int = Field(default=100, ge=0, le=500)
    # Logical aggregate operations are charged before dispatch. This keeps a
    # wide request bounded even when the adapter combines measures in one
    # fixed statement per table.
    postgres_profile_max_operations_per_run: int = Field(default=250, ge=1, le=1_000)
    postgres_profile_max_concurrent_operations: int = Field(default=1, ge=1, le=5)
    source_profile_worker_max_concurrent_jobs: int = Field(default=1, ge=1, le=5)
    source_profile_worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=30)
    source_profile_worker_lease_seconds: float = Field(default=45.0, ge=5, le=300)
    source_profile_worker_lease_renewal_seconds: float = Field(default=5.0, ge=1, le=60)
    source_profile_worker_max_attempts: int = Field(default=2, ge=1, le=5)
    source_profile_worker_retry_base_seconds: float = Field(default=5.0, gt=0, le=60)
    source_profile_worker_retry_max_seconds: float = Field(default=30.0, gt=0, le=300)

    # --- frontend ---
    frontend_url: str = "http://localhost:5173"
    # Comma-separated browser origins permitted to receive OAuth callbacks.
    # Keep the deployed custom domain here; OAuth never falls back to a Vercel
    # deployment URL.
    frontend_allowed_origins: str = "http://localhost:5173,https://tryneuronova.in"
    public_url: str = "http://localhost:8000"

    # --- email delivery ---
    # Preferred in hosted environments: Resend's HTTPS API (port 443). This
    # avoids SMTP ports that are blocked by some hosting providers.
    resend_api_key: str = ""

    # SMTP is retained as a fallback for environments without a Resend key.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@neuronova.ai"
    # SMTP_TLS enables encrypted SMTP. Port 465 uses implicit TLS; other
    # standard submission ports (including Gmail's 587) use STARTTLS.
    smtp_tls: bool = True

    @field_validator("postgres_profile_sample_percent")
    @classmethod
    def validate_profile_sample_precision(cls, value: float) -> float:
        """Keep stored sampling provenance identical to remote parameters.

        Profile runs persist this setting in ``NUMERIC(7, 4)`` and pass it to
        the fixed PostgreSQL TABLESAMPLE statement. Reject extra precision
        instead of letting a database round the persisted configuration to a
        different percentage than the owner requested/configured.
        """

        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("profile sample percent is invalid") from exc
        if decimal_value.as_tuple().exponent < -4:
            raise ValueError("profile sample percent supports at most four decimal places")
        return float(decimal_value)

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

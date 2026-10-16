"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.
"""

from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# ``NoDecode`` keeps pydantic-settings from JSON-decoding these env values:
# operators provide them as CSV (``SECURITY_BLOCKED_TABLES=payments,secrets``)
# and the ``mode="before"`` validator below turns them into a list.
StrList = Annotated[list[str], NoDecode]


def _parse_str_list(v: Any) -> Any:
    """Parse a comma-separated string or JSON list into a list of strings.

    Args:
        v: Raw value coming from environment variables or Python code.

    Returns:
        Any: Normalized list when the input is a string, otherwise the value
        unchanged so that Pydantic can validate it normally.
    """
    if isinstance(v, str):
        raw = v.strip()
        if not raw:
            return []
        # Accept a JSON array (e.g. '["users","payments"]') as well as CSV.
        if raw.startswith("["):
            import json

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]
    return v


class DatabaseConfig(BaseSettings):
    """PostgreSQL database connection configuration.

    ``name`` is both the logical identifier used by callers (the ``database``
    argument of the query tool) and the physical database name to connect to.
    """

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    name: str = Field(default="postgres", description="Logical and physical database name")
    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")
    ssl_mode: str = Field(
        default="prefer",
        description="SSL mode: disable|allow|prefer|require|verify-ca|verify-full",
    )

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @field_validator("ssl_mode")
    @classmethod
    def validate_ssl_mode(cls, v: str) -> str:
        """Validate SSL mode against the modes supported by PostgreSQL."""
        allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        if v not in allowed:
            raise ValueError(f"ssl_mode must be one of {sorted(allowed)}")
        return v

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


class OpenAIConfig(BaseSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(default=2000, ge=100, le=4096, description="Maximum tokens in response")
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Validate API key is not empty and has correct format."""
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        if not api_key_str.startswith("sk-"):
            raise ValueError("OpenAI API key must start with 'sk-'")
        return v


class SecurityConfig(BaseSettings):
    """Security and access control configuration."""

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    allow_write_operations: bool = Field(
        default=False, description="Allow write operations (INSERT, UPDATE, DELETE)"
    )
    blocked_functions: StrList = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    blocked_tables: StrList = Field(
        default_factory=list,
        description=(
            "Sensitive tables that must never be queried. Accepts either bare "
            "names ('payments') or schema-qualified names ('public.payments')."
        ),
    )
    blocked_columns: StrList = Field(
        default_factory=list,
        description=(
            "Sensitive columns that must never be selected. Accepts bare names "
            "('password_hash') or qualified names ('users.password_hash')."
        ),
    )
    allow_explain: bool = Field(
        default=False,
        description="Allow EXPLAIN statements (read-only plan inspection)",
    )
    block_system_schemas: bool = Field(
        default=True,
        description=(
            "Block queries reading PostgreSQL system schemas (pg_catalog, "
            "information_schema, pg_toast, pg_temp)"
        ),
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    enforce_row_limit: bool = Field(
        default=True,
        description=(
            "Push max_rows into the SQL as a LIMIT so the database stops early "
            "instead of streaming every row into memory"
        ),
    )
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )

    @field_validator("blocked_functions", "blocked_tables", "blocked_columns", mode="before")
    @classmethod
    def parse_str_lists(cls, v: Any) -> Any:
        """Parse comma-separated strings (``A,B``) or JSON arrays into lists."""
        return _parse_str_list(v)

    @property
    def query_timeout_seconds(self) -> float:
        """Alias matching the design document naming (``query_timeout_seconds``)."""
        return self.max_execution_time

    @property
    def max_result_rows(self) -> int:
        """Alias matching the design document naming (``max_result_rows``)."""
        return self.max_rows


class AuthPrincipal(BaseSettings):
    """One authenticated caller and the databases it may reach."""

    name: str = Field(description="Human-readable principal name")
    api_key: SecretStr = Field(description="Secret token presented by the caller")
    roles: StrList = Field(
        default_factory=lambda: ["reader"],
        description="Role labels carried on the audit record",
    )
    allowed_databases: StrList = Field(
        default_factory=list,
        description="Databases this principal may query; empty means all",
    )


class AuthConfig(BaseSettings):
    """Authentication / authorization settings.

    Disabled by default so existing single-user deployments keep working;
    enabling it turns on per-database access control and audit logging
    (design review 0003, finding #1).
    """

    enabled: bool = Field(default=False, description="Enable API-key authentication")
    principals: list[AuthPrincipal] = Field(
        default_factory=list, description="Configured callers"
    )
    audit_log_enabled: bool = Field(
        default=True, description="Log an audit record for every request"
    )


class ValidationConfig(BaseSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(env_prefix="VALIDATION_")

    max_question_length: int = Field(
        default=10000, ge=1, le=50000, description="Maximum question length in characters"
    )
    min_confidence_score: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence score (0-100)"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    mask_sensitive_data: bool = Field(
        default=True,
        description=(
            "Mask sensitive columns before sampling rows to the validator LLM"
        ),
    )
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence for acceptable results"
    )


class CacheConfig(BaseSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")


class ResilienceConfig(BaseSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(env_prefix="RESILIENCE_")

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description="Initial retry delay in seconds (small values are useful in tests)",
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    max_retry_delay: float = Field(
        default=30.0, ge=0.1, le=300.0, description="Upper bound for a single retry delay"
    )
    retry_jitter: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Fraction of random jitter applied to each retry delay (0-1)",
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )

    # Rate limiting
    max_concurrent_queries: int = Field(
        default=10, ge=1, le=500, description="Maximum concurrent database queries"
    )
    max_concurrent_llm_calls: int = Field(
        default=5, ge=1, le=500, description="Maximum concurrent LLM API calls"
    )
    acquire_timeout: float = Field(
        default=30.0,
        ge=0.1,
        le=600.0,
        description="Seconds to wait for a rate limiter slot before rejecting the request",
    )


class ObservabilityConfig(BaseSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_")

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(
        default="json", description="Log format (json in production, text for local runs)"
    )


class Settings(BaseSettings):
    """Main application settings aggregating all config sections.

    Multi-database configuration is available through ``databases``. It can be
    provided either as JSON (``DATABASES=[{...}]``) or through the nested
    environment syntax (``DATABASES__0__NAME=...``). When ``databases`` is
    empty the legacy single-database block (``DATABASE_*``) is used as a
    one-element fallback so that existing deployments keep working.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )

    # Nested configurations
    databases: list[DatabaseConfig] = Field(
        default_factory=list,
        description="Databases to serve; falls back to `database` when empty",
    )
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @model_validator(mode="after")
    def validate_databases(self) -> "Settings":
        """Ensure at least one database is configured and names are unique.

        Returns:
            Settings: The validated settings instance.

        Raises:
            ValueError: If the configured database names are not unique.
        """
        if not self.databases:
            # Backward compatibility: legacy single database configuration.
            self.databases = [self.database]

        names = [db.name for db in self.databases]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"Database names must be unique, duplicated: {sorted(duplicates)}")
        return self

    def get_database(self, name: str) -> DatabaseConfig | None:
        """Look up a database configuration by its logical name.

        Args:
            name: Logical database name.

        Returns:
            DatabaseConfig | None: The matching configuration if present.
        """
        for db in self.databases:
            if db.name == name:
                return db
        return None

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None

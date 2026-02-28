"""
Jira Cortex - Pydantic Settings Configuration

Centralized, type-safe configuration management with validation.
All secrets loaded from environment variables only.
"""

from functools import lru_cache
from typing import List, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings with strict validation.
    All sensitive values must come from environment variables.
    """
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    # -----------------------------------------
    # OpenAI Configuration
    # -----------------------------------------
    openai_api_key: str = Field(..., min_length=20, description="OpenAI API Key")
    openai_embedding_model: str = Field(default="text-embedding-3-small")
    openai_chat_model: str = Field(default="gpt-4o")
    openai_max_tokens: int = Field(default=4096, ge=100, le=128000)
    openai_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    
    # -----------------------------------------
    # Qdrant Configuration
    # -----------------------------------------
    qdrant_url: str = Field(..., description="Qdrant cluster URL")
    qdrant_api_key: Optional[str] = Field(default=None, description="Qdrant API Key")
    qdrant_collection_name: str = Field(default="jira_cortex_docs")
    
    # -----------------------------------------
    # Redis Configuration
    # -----------------------------------------
    redis_url: str = Field(default="redis://localhost:6379/0")
    redis_password: Optional[str] = Field(default=None)
    redis_cache_ttl_seconds: int = Field(default=10, ge=1, le=3600)
    
    # -----------------------------------------
    # Atlassian Configuration
    # -----------------------------------------
    atlassian_client_id: str = Field(..., description="Atlassian OAuth Client ID")
    atlassian_client_secret: str = Field(..., min_length=10, description="Atlassian OAuth Client Secret")
    atlassian_base_url: str = Field(default="https://api.atlassian.com")
    
    # -----------------------------------------
    # Security Configuration
    # -----------------------------------------
    jwt_secret_key: str = Field(..., min_length=32, description="JWT signing secret")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expiry_minutes: int = Field(default=60, ge=5, le=1440)
    cors_allowed_origins: str = Field(default="https://*.atlassian.net")
    rate_limit_requests_per_minute: int = Field(default=60, ge=10, le=1000)
    
    # -----------------------------------------
    # Application Configuration
    # -----------------------------------------
    app_env: str = Field(default="development")
    app_debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    
    # -----------------------------------------
    # Ingestion Configuration
    # -----------------------------------------
    ingestion_batch_size: int = Field(default=50, ge=1, le=100)
    chunk_size_tokens: int = Field(default=500, ge=100, le=2000)
    max_chunks_per_query: int = Field(default=3, ge=1, le=10)
    
    # -----------------------------------------
    # Usage Tracking
    # -----------------------------------------
    enable_usage_tracking: bool = Field(default=True)
    database_url: Optional[str] = Field(default=None)
    
    # -----------------------------------------
    # Tenant Gating (Commercial)
    # -----------------------------------------
    # Comma-separated list of allowed tenant IDs. Empty = dev mode allows all.
    allowed_tenants: str = Field(default="", description="Comma-separated allowed tenant cloud IDs")
    
    # -----------------------------------------
    # Stripe (Payments)
    # -----------------------------------------
    stripe_secret_key: Optional[str] = Field(default=None, description="Stripe Secret API Key (sk_...)")
    stripe_webhook_secret: Optional[str] = Field(default=None, description="Stripe Webhook Signing Secret (whsec_...)")
    stripe_platform_price_id: Optional[str] = Field(default=None, description="Stripe Price ID for $299/mo subscription")
    app_frontend_url: str = Field(default="https://your-jira-instance.atlassian.net", description="Frontend URL for Stripe redirects")
    
    @field_validator("app_env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v.lower() not in allowed:
            raise ValueError(f"app_env must be one of {allowed}")
        return v.lower()
    
    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v.upper()
    
    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS origins from comma-separated string."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",")]
    
    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.app_env == "production"


@lru_cache()
def get_settings() -> Settings:
    """
    Cached settings loader.
    Settings are loaded once and reused for performance.
    """
    return Settings()

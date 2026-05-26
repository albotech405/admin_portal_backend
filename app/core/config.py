from typing import List
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file: backend/app/core/config.py -> backend/.env
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    APP_NAME: str = Field(default="AlboTax Backend")
    DEBUG: bool = Field(default=False)
    ENVIRONMENT: str = Field(default="development")
    API_V1_PREFIX: str = Field(default="/api/v1")

    SUPABASE_URL: str
    SUPABASE_KEY: str
    SUPABASE_JWT_SECRET: str
    SUPABASE_SERVICE_KEY: str
    DATABASE_URL: str

    @field_validator("SUPABASE_JWT_SECRET", mode="before")
    @classmethod
    def strip_jwt_secret_quotes(cls, v: str) -> str:
        """Strip accidental surrounding quote characters (common Render env-var mistake)."""
        if isinstance(v, str):
            return v.strip("\"'")
        return v

    FIREBASE_SERVICE_ACCOUNT_JSON: str = Field(default="{}")
    FIREBASE_SERVICE_ACCOUNT_PATH: str = Field(default="")
    FIREBASE_PROJECT_ID: str = Field(default="")

    LOW_BALANCE_THRESHOLD: float = Field(default=500.0)

    ALLOWED_ORIGINS: str = Field(default="http://localhost:3000,http://localhost:3001,http://localhost:5173")

    # Publicly reachable base URL of this service (used to build tracking URLs).
    # Override with the actual deployment URL in production, e.g. https://api.albotax.com
    PUBLIC_BASE_URL: str = Field(default="http://localhost:8000")

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")


settings = Settings()

from typing import List
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file: backend/app/core/config.py -> backend/.env
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"
_DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:5173",
]


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
    SOS_ADMIN_NOTIFICATION_ROLES: str = Field(default="operations,super_admin")
    SOS_NEARBY_DRIVER_RADIUS_KM: float = Field(default=5.0)
    SOS_NEARBY_DRIVER_ENABLED: bool = Field(default=True)
    SOS_RESPONSE_INSTRUCTIONS: str = Field(default="Open the driver app and follow the emergency assistance flow.")
    SOS_STALE_AFTER_SECONDS: int = Field(default=120)
    SOS_SHARE_DURATION_MINUTES: int = Field(default=60)
    SOS_ALLOWED_SHARE_DURATIONS_MINUTES: str = Field(default="15,60,480")
    SOS_ROUTE_HISTORY_LIMIT: int = Field(default=50)

    ALLOWED_ORIGINS: str = Field(default="http://localhost:3000,http://localhost:3001,http://localhost:5173")
    ADMIN_FRONTEND_ORIGINS: str = Field(default="")

    # Publicly reachable base URL of this service (used to build tracking URLs).
    # Override with the actual deployment URL in production, e.g. https://api.albotax.com
    PUBLIC_BASE_URL: str = Field(default="http://localhost:8000")

    @property
    def cors_origins(self) -> List[str]:
        values = [*self.ALLOWED_ORIGINS.split(","), *self.ADMIN_FRONTEND_ORIGINS.split(",")]
        normalized: List[str] = []
        seen: set[str] = set()
        for raw_origin in [*_DEFAULT_CORS_ORIGINS, *values]:
            origin = raw_origin.strip().rstrip("/")
            if not origin or origin in seen:
                continue
            seen.add(origin)
            normalized.append(origin)
        return normalized

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")


settings = Settings()

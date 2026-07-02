from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, SecretStr


class Settings(BaseSettings):
    # --- Core system requirements ---
    DATABASE_URL: str

    # --- AI Core (Groq) ---
    GROQ_API_KEY: SecretStr

    # --- Telegram Bot ---
    TELEGRAM_BOT_TOKEN: SecretStr
    WEBHOOK_URL: str
    WEBHOOK_SECRET_TOKEN: SecretStr

    # --- Auto-Healer (GitHub + Render) ---
    GITHUB_TOKEN: SecretStr
    REPO_NAME: str

    # --- Admin control (self-modify command ke liye — SIRF yahi ID command chala payega) ---
    ADMIN_TELEGRAM_ID: int

    # --- Runtime info (health check ke liye) ---
    ENVIRONMENT: str = "production"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def fix_postgres_protocol(cls, v: str) -> str:
        if not v:
            return v
        # Agar URL postgres:// ya postgresql:// se shuru ho raha hai, toh use asyncpg par force karein
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        # Agar galti se psycopg2 jod gaya ho, toh use hata kar asyncpg karein
        elif "postgresql+psycopg2://" in v:
            v = v.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",       # unknown env vars ko silently ignore karo
        case_sensitive=True,  # env var names exact case match hone chahiye
    )


settings = Settings()

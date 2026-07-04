from typing import Optional
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

    # --- Render API (manual deploy trigger + deploy status polling ke liye) ---
    # Render Dashboard > Account Settings > API Keys se milega
    RENDER_API_KEY: Optional[SecretStr] = None
    # Service URL me dikhta hai: srv-xxxxxxxxxxxx
    RENDER_SERVICE_ID: Optional[str] = None

    # --- Force-Subscribe / Growth features ---
    # Public channel ka username, e.g. "@mychannel" (bina @ ke bhi chalega, code handle karta hai)
    CHANNEL_USERNAME: Optional[str] = None
    FORCE_SUBSCRIBE_ENABLED: bool = False  # channel set hone tak False rahega (safety default)

    # Free promotional website ka link (GitHub Pages / any free host)
    WEBSITE_URL: Optional[str] = None

    # --- Stability features ---
    AUTO_RESTART_MINUTES: int = 30       # process ko har N min me gracefully restart karega
    AUTO_RESTART_ENABLED: bool = True
    KEEPALIVE_ENABLED: bool = True       # Render free-tier ko sleep hone se rokne ke liye self-ping
    KEEPALIVE_INTERVAL_MINUTES: int = 10

    @field_validator("CHANNEL_USERNAME", mode="before")
    @classmethod
    def normalize_channel_username(cls, v):
        if not v:
            return v
        v = v.strip()
        return v if v.startswith("@") else f"@{v}"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def fix_postgres_protocol(cls, v: str) -> str:
        if not v:
            return v

        # --- Mobile copy-paste se aksar invisible characters (spaces, newlines,
        # smart-quotes, zero-width chars) chale aate hain jo screen par dikhte nahi,
        # lekin hostname ke andar chip kar DNS resolution fail kara dete hain
        # ("Name or service not known" wala error). Pehle unhe strip/clean kar dete hain.
        original = v
        v = v.strip()
        # Sirf printable, non-whitespace ASCII allow karte hain URL ke andar
        v = "".join(ch for ch in v if ch.isprintable() and not ch.isspace())
        if v != original:
            import structlog
            structlog.get_logger(__name__).warning(
                "DATABASE_URL mein invalid/hidden characters mile — auto-cleaned.",
                original_length=len(original),
                cleaned_length=len(v),
            )

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

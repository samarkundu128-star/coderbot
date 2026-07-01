 from pydantic_settings import BaseSettings
from pydantic import field_validator

class Settings(BaseSettings):
    # Aapki baaki saari fields yahan hongi...
    DATABASE_URL: str

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

settings = Settings()

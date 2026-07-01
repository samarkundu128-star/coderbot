```python
import os
from typing import Literal
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

    ENVIRONMENT: Literal["development", "production", "testing"] = "production"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    SECRET_KEY: SecretStr

    TELEGRAM_BOT_TOKEN: SecretStr
    WEBHOOK_URL: str
    WEBHOOK_SECRET_TOKEN: SecretStr

    DATABASE_URL: SecretStr
    GEMINI_API_KEY: SecretStr

    @field_validator("WEBHOOK_URL")
    @classmethod
    def validate_webhook_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("WEBHOOK_URL hamesha 'https://' ke sath hona chahiye secure deployment ke liye.")
        return v.rstrip("/")

settings = AppSettings()

```

import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, HttpUrl

class Settings(BaseSettings):
    # App Run Mode & Logging configurations
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"
    
    # Internal encryption security key
    SECRET_KEY: SecretStr
    
    # Core Network credentials
    TELEGRAM_BOT_TOKEN: SecretStr
    WEBHOOK_URL: HttpUrl
    WEBHOOK_SECRET_TOKEN: SecretStr
    DATABASE_URL: str
    GEMINI_API_KEY: SecretStr

    # Configuration for feeding environment keys safely
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

# Settings configuration instance ready for import
settings = Settings()

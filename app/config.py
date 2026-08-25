from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "postgresql+asyncpg://news:news@localhost:5432/news"
    telegram_api_id: int = Field(gt=0)
    telegram_api_hash: SecretStr
    telegram_phone_number: str
    telegram_database_encryption_key: SecretStr = SecretStr("")
    tdlib_data_dir: Path = Path("./tdlib-data")
    tdlib_library_path: Path | None = None
    tdlib_log_verbosity: int = Field(default=2, ge=0, le=1024)
    telegram_download_media: bool = False
    telegram_media_download_priority: int = Field(default=16, ge=1, le=32)
    log_level: str = "INFO"
    database_retry_initial_seconds: float = Field(default=1.0, gt=0)
    database_retry_max_seconds: float = Field(default=30.0, gt=0)

    @field_validator("telegram_api_hash", "telegram_phone_number")
    @classmethod
    def value_must_not_be_blank(cls, value: SecretStr | str) -> SecretStr | str:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

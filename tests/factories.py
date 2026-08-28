from pathlib import Path

from app.config import Settings


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://news:database-secret@postgres:5432/news",
        "telegram_api_id": 12345,
        "telegram_api_hash": "api-secret",
        "telegram_phone_number": "+10000000000",
        "telegram_database_encryption_key": "tdlib-secret",
        "tdlib_data_dir": Path("/tmp/news-tdlib"),
        "tdlib_library_path": Path("/usr/local/lib/libtdjson.so"),
    }
    values.update(overrides)
    return Settings.model_validate(values)

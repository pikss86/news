from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    FIELD_METADATA: ClassVar[dict[str, dict[str, Any]]] = {
        "database_url": {
            "section": "PostgreSQL",
            "label": "Адрес PostgreSQL",
            "secret": True,
            "apply": "collector-restart",
            "placeholder": "Оставьте пустым для встроенной базы",
            "help": (
                "Для обычного запуска ничего вводить не нужно: внутренний адрес и пароль уже "
                "созданы автоматически. Заполняйте только для внешней PostgreSQL; формат: "
                "postgresql+asyncpg://user:password@host:5432/database."
            ),
        },
        "telegram_api_id": {
            "section": "Telegram",
            "label": "Telegram API ID",
            "secret": False,
            "apply": "collector-restart",
            "placeholder": "Например: 12345678",
            "help": (
                "Обязательное числовое значение. Получите на my.telegram.org → API development "
                "tools после создания приложения. Это не ID канала и не ID бота."
            ),
        },
        "telegram_api_hash": {
            "section": "Telegram",
            "label": "Telegram API hash",
            "secret": True,
            "apply": "collector-restart",
            "placeholder": "Строка из my.telegram.org",
            "help": (
                "Обязательный секрет из той же карточки приложения на my.telegram.org. "
                "Не используйте bot token. После сохранения значение больше не показывается."
            ),
        },
        "telegram_phone_number": {
            "section": "Telegram",
            "label": "Номер Telegram-аккаунта",
            "secret": True,
            "apply": "collector-restart",
            "placeholder": "+79991234567",
            "help": (
                "Обязательный номер аккаунта, подписанного на нужные каналы. Введите в "
                "международном формате с плюсом и кодом страны. Код входа появится позже "
                "отдельной формой и здесь не сохраняется."
            ),
        },
        "telegram_database_encryption_key": {
            "section": "Telegram",
            "label": "Ключ локальной базы Telegram",
            "secret": True,
            "apply": "collector-restart",
            "placeholder": "Можно оставить пустым",
            "help": (
                "Необязательная собственная длинная строка для локального состояния Telegram. "
                "Если зададите её, сохраните резервную копию и больше не меняйте: без прежнего "
                "ключа существующая сессия не откроется."
            ),
        },
        "tdlib_data_dir": {
            "section": "TDLib",
            "label": "Каталог состояния Telegram",
            "secret": False,
            "apply": "collector-restart",
            "placeholder": "/var/lib/tdlib",
            "help": (
                "Постоянное хранилище авторизации и загруженных файлов. В Docker оставьте "
                "/var/lib/tdlib — этот путь подключён к persistent volume."
            ),
        },
        "tdlib_library_path": {
            "section": "TDLib",
            "label": "Путь к TDLib",
            "secret": False,
            "apply": "collector-restart",
            "placeholder": "/usr/local/lib/libtdjson.so",
            "help": (
                "Путь к библиотеке Telegram внутри контейнера. Для стандартного Docker-образа "
                "оставьте /usr/local/lib/libtdjson.so."
            ),
        },
        "tdlib_log_verbosity": {
            "section": "TDLib",
            "label": "Подробность логов TDLib",
            "secret": False,
            "apply": "dynamic",
            "placeholder": "2",
            "help": (
                "Рекомендуется 2. Значение 0 отключает почти все логи; большие значения нужны "
                "только для диагностики."
            ),
        },
        "telegram_download_media": {
            "section": "Media",
            "label": "Скачивать медиафайлы",
            "secret": False,
            "apply": "dynamic",
            "placeholder": "",
            "help": (
                "Включите, чтобы сохранять фотографии, видео и документы, а не только их "
                "описания. Ограничения по размеру пока нет — контролируйте свободное место."
            ),
        },
        "telegram_media_download_priority": {
            "section": "Media",
            "label": "Приоритет скачивания",
            "secret": False,
            "apply": "dynamic",
            "placeholder": "16",
            "help": (
                "Число от 1 до 32. Чем больше число, тем раньше Telegram поставит файл на "
                "скачивание; обычно оставьте 16."
            ),
        },
        "log_level": {
            "section": "Logging",
            "label": "Уровень журналирования",
            "secret": False,
            "apply": "dynamic",
            "placeholder": "INFO",
            "help": (
                "Обычно INFO. DEBUG даёт больше технических сообщений; WARNING и ERROR "
                "скрывают обычный ход работы."
            ),
        },
        "database_retry_initial_seconds": {
            "section": "Reliability",
            "label": "Начальная задержка повтора, секунд",
            "secret": False,
            "apply": "collector-restart",
            "placeholder": "1",
            "help": (
                "Сколько ждать перед первым повтором при временной недоступности PostgreSQL. "
                "Обычно оставьте 1."
            ),
        },
        "database_retry_max_seconds": {
            "section": "Reliability",
            "label": "Максимальная задержка повтора, секунд",
            "secret": False,
            "apply": "collector-restart",
            "placeholder": "30",
            "help": (
                "Верхний предел увеличивающейся задержки между повторами. Должен быть не "
                "меньше начальной задержки."
            ),
        },
    }

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
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("database_url")
    @classmethod
    def database_url_must_be_async_postgresql(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("must use postgresql+asyncpg://")
        return value

    @model_validator(mode="after")
    def retry_delays_are_consistent(self) -> "Settings":
        if self.database_retry_max_seconds < self.database_retry_initial_seconds:
            raise ValueError("database retry maximum must be >= initial delay")
        return self

    @classmethod
    def secret_field_names(cls) -> frozenset[str]:
        return frozenset(
            name for name, metadata in cls.FIELD_METADATA.items() if metadata["secret"]
        )

    def plain_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                values[name] = value.get_secret_value()
            elif isinstance(value, Path):
                values[name] = str(value)
            else:
                values[name] = value
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

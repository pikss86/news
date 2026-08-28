from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = frozenset(
    {
        "api_hash",
        "code",
        "database_url",
        "encryption_key",
        "password",
        "phone_number",
        "telegram_api_hash",
        "telegram_database_encryption_key",
        "telegram_phone_number",
    }
)
POSTGRES_URL = re.compile(r"(postgres(?:ql)?(?:\+asyncpg)?://[^:/\s]+:)([^@\s]+)(@)")
PHONE_NUMBER = re.compile(r"(?<![\w+])\+[0-9][0-9 ()-]{6,18}[0-9]")


def redact_text(value: str) -> str:
    return PHONE_NUMBER.sub(REDACTED, POSTGRES_URL.sub(rf"\1{REDACTED}\3", value))


def redact(value: Any, key: str | None = None) -> Any:
    if key and key.lower() in SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {item_key: redact(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value

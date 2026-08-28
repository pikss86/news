from __future__ import annotations

import argparse
import base64
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapSecrets:
    postgres_password_file: Path
    settings_key_file: Path
    control_key_file: Path

    @property
    def postgres_password(self) -> str:
        return self.postgres_password_file.read_text(encoding="utf-8").strip()


def _atomic_create(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _validate_password(value: bytes) -> None:
    if len(value.strip()) < 32:
        raise BootstrapError("existing PostgreSQL password secret is invalid")


def _validate_fernet_key(value: bytes) -> None:
    try:
        decoded = base64.urlsafe_b64decode(value.strip())
    except Exception as error:
        raise BootstrapError("existing settings encryption key is invalid") from error
    if len(decoded) != 32:
        raise BootstrapError("existing settings encryption key is invalid")


def _validate_control_key(value: bytes) -> None:
    try:
        decoded = bytes.fromhex(value.decode().strip())
    except (ValueError, UnicodeDecodeError) as error:
        raise BootstrapError("existing control signing key is invalid") from error
    if len(decoded) != 32:
        raise BootstrapError("existing control signing key is invalid")


def _ensure(path: Path, factory: Callable[[], bytes], validator: Callable[[bytes], None]) -> None:
    if not path.exists():
        try:
            _atomic_create(path, factory())
        except FileExistsError:
            pass
    data = path.read_bytes()
    validator(data)
    path.chmod(0o600)


def ensure_bootstrap(directory: Path) -> BootstrapSecrets:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    result = BootstrapSecrets(
        postgres_password_file=directory / "postgres_password",
        settings_key_file=directory / "settings.key",
        control_key_file=directory / "control.key",
    )
    _ensure(
        result.postgres_password_file,
        lambda: (secrets.token_urlsafe(36) + "\n").encode(),
        _validate_password,
    )
    _ensure(result.settings_key_file, lambda: Fernet.generate_key() + b"\n", _validate_fernet_key)
    _ensure(
        result.control_key_file,
        lambda: (secrets.token_hex(32) + "\n").encode(),
        _validate_control_key,
    )
    return result


def load_bootstrap(directory: Path) -> BootstrapSecrets:
    result = BootstrapSecrets(
        postgres_password_file=directory / "postgres_password",
        settings_key_file=directory / "settings.key",
        control_key_file=directory / "control.key",
    )
    try:
        _validate_password(result.postgres_password_file.read_bytes())
        _validate_fernet_key(result.settings_key_file.read_bytes())
        _validate_control_key(result.control_key_file.read_bytes())
    except OSError as error:
        raise BootstrapError("bootstrap secrets are unavailable") from error
    return result


def bundled_database_url(secrets_directory: Path, host: str = "postgres") -> str:
    password = load_bootstrap(secrets_directory).postgres_password
    return f"postgresql+asyncpg://news:{password}@{host}:5432/news"


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize persistent service secrets")
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    ensure_bootstrap(arguments.directory)


if __name__ == "__main__":
    main()

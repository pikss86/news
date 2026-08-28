from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings


class SettingsStoreError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked_file(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class SettingsStore:
    REQUIRED_CHECKS = frozenset({"postgresql", "migrations", "tdlib", "storage", "telegram"})

    def __init__(self, directory: Path, key_file: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        try:
            self._fernet = Fernet(key_file.read_bytes().strip())
        except (OSError, ValueError) as error:
            raise SettingsStoreError("settings encryption key is unavailable or invalid") from error
        self.manifest_path = directory / "manifest.json"
        self.revisions_directory = directory / "revisions"
        self.lock_path = directory / ".store.lock"

    @staticmethod
    def _empty_manifest() -> dict[str, Any]:
        return {
            "format": 1,
            "next_revision": 1,
            "draft_revision": None,
            "active_revision": None,
            "applied_revision": None,
            "revisions": [],
            "checks": None,
        }

    def _load_manifest_unlocked(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return self._empty_manifest()
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SettingsStoreError("settings manifest is damaged") from error
        if manifest.get("format") != 1 or not isinstance(manifest.get("revisions"), list):
            raise SettingsStoreError("settings manifest has an unsupported format")
        return manifest

    def manifest(self) -> dict[str, Any]:
        with locked_file(self.lock_path):
            return self._load_manifest_unlocked()

    def _save_manifest_unlocked(self, manifest: dict[str, Any]) -> None:
        atomic_write(self.manifest_path, canonical_json(manifest) + b"\n")

    def _revision_path(self, revision: int) -> Path:
        return self.revisions_directory / f"{revision:08d}.bin"

    def _load_revision_unlocked(self, revision: int) -> tuple[Settings, dict[str, Any]]:
        try:
            plaintext = self._fernet.decrypt(self._revision_path(revision).read_bytes())
            document = json.loads(plaintext)
            settings = Settings.model_validate(document["settings"])
        except (OSError, InvalidToken, KeyError, ValueError, json.JSONDecodeError) as error:
            raise SettingsStoreError(f"settings revision {revision} cannot be decrypted") from error
        return settings, document

    def load_revision(self, revision: int) -> Settings:
        with locked_file(self.lock_path):
            return self._load_revision_unlocked(revision)[0]

    def load_draft(self) -> Settings | None:
        with locked_file(self.lock_path):
            manifest = self._load_manifest_unlocked()
            revision = manifest.get("draft_revision")
            return self._load_revision_unlocked(revision)[0] if revision is not None else None

    def load_active(self) -> Settings | None:
        with locked_file(self.lock_path):
            manifest = self._load_manifest_unlocked()
            revision = manifest.get("active_revision")
            return self._load_revision_unlocked(revision)[0] if revision is not None else None

    @staticmethod
    def settings_hash(settings: Settings) -> str:
        return hashlib.sha256(canonical_json(settings.plain_dict())).hexdigest()

    def save_draft(self, settings: Settings, *, source: str = "admin") -> int:
        with locked_file(self.lock_path):
            manifest = self._load_manifest_unlocked()
            previous: Settings | None = None
            if manifest.get("draft_revision") is not None:
                previous = self._load_revision_unlocked(manifest["draft_revision"])[0]
            previous_values = previous.plain_dict() if previous else {}
            current_values = settings.plain_dict()
            changed_fields = sorted(
                name for name, value in current_values.items() if previous_values.get(name) != value
            )
            revision = int(manifest["next_revision"])
            created_at = datetime.now(UTC).isoformat()
            document = {
                "revision": revision,
                "created_at": created_at,
                "source": source,
                "settings": current_values,
            }
            encrypted = self._fernet.encrypt(canonical_json(document))
            atomic_write(self._revision_path(revision), encrypted)
            manifest["next_revision"] = revision + 1
            manifest["draft_revision"] = revision
            manifest["checks"] = None
            manifest["revisions"].append(
                {
                    "revision": revision,
                    "created_at": created_at,
                    "source": source,
                    "changed_fields": changed_fields,
                }
            )
            self._save_manifest_unlocked(manifest)
            return revision

    def list_revisions(self) -> list[dict[str, Any]]:
        return list(reversed(self.manifest()["revisions"]))

    def rollback_to_draft(self, revision: int) -> int:
        with locked_file(self.lock_path):
            settings = self._load_revision_unlocked(revision)[0]
        return self.save_draft(settings, source=f"rollback:{revision}")

    def save_checks(self, revision: int, results: dict[str, dict[str, Any]]) -> None:
        with locked_file(self.lock_path):
            manifest = self._load_manifest_unlocked()
            if manifest.get("draft_revision") != revision:
                raise SettingsStoreError("check results do not match the current draft")
            settings = self._load_revision_unlocked(revision)[0]
            manifest["checks"] = {
                "revision": revision,
                "settings_hash": self.settings_hash(settings),
                "checked_at": datetime.now(UTC).isoformat(),
                "results": results,
            }
            self._save_manifest_unlocked(manifest)

    def checks_passed(self, revision: int | None = None) -> bool:
        with locked_file(self.lock_path):
            manifest = self._load_manifest_unlocked()
            target = revision if revision is not None else manifest.get("draft_revision")
            checks = manifest.get("checks")
            if target is None or not checks or checks.get("revision") != target:
                return False
            settings = self._load_revision_unlocked(target)[0]
            if checks.get("settings_hash") != self.settings_hash(settings):
                return False
            results = checks.get("results", {})
            return all(results.get(name, {}).get("ok") is True for name in self.REQUIRED_CHECKS)

    def activate_draft(self) -> int:
        with locked_file(self.lock_path):
            manifest = self._load_manifest_unlocked()
            revision = manifest.get("draft_revision")
            if revision is None:
                raise SettingsStoreError("there is no draft to activate")
            checks = manifest.get("checks")
            settings = self._load_revision_unlocked(revision)[0]
            if (
                not checks
                or checks.get("revision") != revision
                or checks.get("settings_hash") != self.settings_hash(settings)
            ):
                raise SettingsStoreError("the current draft has no valid checks")
            results = checks.get("results", {})
            if not all(results.get(name, {}).get("ok") is True for name in self.REQUIRED_CHECKS):
                raise SettingsStoreError("mandatory checks have not passed")
            manifest["active_revision"] = revision
            self._save_manifest_unlocked(manifest)
            return revision

    def mark_applied(self, revision: int) -> None:
        with locked_file(self.lock_path):
            manifest = self._load_manifest_unlocked()
            if manifest.get("active_revision") != revision:
                raise SettingsStoreError("only the active revision can be marked applied")
            manifest["applied_revision"] = revision
            self._save_manifest_unlocked(manifest)

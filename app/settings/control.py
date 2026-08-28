from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.settings.store import SettingsStoreError, atomic_write, canonical_json, locked_file

ControlAction = Literal["start", "stop", "restart", "download_file", "load_chat_history"]
ALLOWED_ACTIONS = frozenset({"start", "stop", "restart", "download_file", "load_chat_history"})


class ControlChannel:
    def __init__(self, directory: Path, key_file: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.key = bytes.fromhex(key_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as error:
            raise SettingsStoreError("control signing key is unavailable or invalid") from error
        if len(self.key) != 32:
            raise SettingsStoreError("control signing key is unavailable or invalid")
        self.control_path = directory / "control.json"
        self.status_path = directory / "status.json"
        self.lock_path = directory / ".control.lock"

    def _sign(self, payload: dict[str, Any]) -> str:
        return hmac.new(self.key, canonical_json(payload), hashlib.sha256).hexdigest()

    def request(
        self,
        action: ControlAction,
        revision: int | None = None,
        *,
        file_id: int | None = None,
        remote_file_id: str | None = None,
        chat_id: int | None = None,
        from_message_id: int | None = None,
        limit: int = 100,
    ) -> int:
        if action not in ALLOWED_ACTIONS:
            raise ValueError("unsupported control action")
        with locked_file(self.lock_path):
            previous = self.read_request(verify=False)
            request_id = int(previous.get("request_id", 0)) + 1 if previous else 1
            payload: dict[str, Any] = {
                "request_id": request_id,
                "action": action,
                "revision": revision,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if action == "download_file":
                if file_id is None or file_id <= 0:
                    raise ValueError("download_file requires a positive file_id")
                payload["file_id"] = file_id
                if remote_file_id:
                    payload["remote_file_id"] = remote_file_id
            if action == "load_chat_history":
                if chat_id is None or chat_id == 0:
                    raise ValueError("load_chat_history requires a non-zero chat_id")
                if from_message_id is None or from_message_id < 0:
                    raise ValueError("load_chat_history requires a non-negative from_message_id")
                if not 1 <= limit <= 100:
                    raise ValueError("chat history limit must be between 1 and 100")
                payload.update(
                    {"chat_id": chat_id, "from_message_id": from_message_id, "limit": limit}
                )
            document = {"payload": payload, "signature": self._sign(payload)}
            atomic_write(self.control_path, canonical_json(document) + b"\n")
            return request_id

    def request_download(self, file_id: int, remote_file_id: str | None = None) -> int:
        return self.request("download_file", file_id=file_id, remote_file_id=remote_file_id)

    def request_chat_history(self, chat_id: int, from_message_id: int, limit: int = 100) -> int:
        return self.request(
            "load_chat_history",
            chat_id=chat_id,
            from_message_id=from_message_id,
            limit=limit,
        )

    def read_request(self, *, verify: bool = True) -> dict[str, Any] | None:
        if not self.control_path.exists():
            return None
        try:
            document = json.loads(self.control_path.read_text(encoding="utf-8"))
            payload = document["payload"]
            signature = document["signature"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise SettingsStoreError("control request is damaged") from error
        if payload.get("action") not in ALLOWED_ACTIONS:
            raise SettingsStoreError("control request has an unsupported action")
        if payload.get("action") == "download_file" and (
            not isinstance(payload.get("file_id"), int) or payload["file_id"] <= 0
        ):
            raise SettingsStoreError("download request has an invalid file id")
        if payload.get("action") == "load_chat_history" and (
            not isinstance(payload.get("chat_id"), int)
            or payload["chat_id"] == 0
            or not isinstance(payload.get("from_message_id"), int)
            or payload["from_message_id"] < 0
            or not isinstance(payload.get("limit"), int)
            or not 1 <= payload["limit"] <= 100
        ):
            raise SettingsStoreError("chat history request is invalid")
        remote_file_id = payload.get("remote_file_id")
        if remote_file_id is not None and (
            not isinstance(remote_file_id, str) or not remote_file_id or len(remote_file_id) > 2048
        ):
            raise SettingsStoreError("download request has an invalid remote file id")
        if verify and not hmac.compare_digest(signature, self._sign(payload)):
            raise SettingsStoreError("control request signature is invalid")
        return payload

    def write_status(self, status: dict[str, Any]) -> None:
        safe_status = {"updated_at": datetime.now(UTC).isoformat(), **status}
        atomic_write(self.status_path, canonical_json(safe_status) + b"\n")

    def read_status(self) -> dict[str, Any]:
        if not self.status_path.exists():
            return {"state": "not_configured", "updated_at": None}
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SettingsStoreError("collector status is damaged") from error

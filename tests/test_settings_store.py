import json
import stat

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.settings.bootstrap import BootstrapError, ensure_bootstrap, load_bootstrap
from app.settings.control import ControlChannel
from app.settings.redaction import REDACTED, redact
from app.settings.store import SettingsStore, SettingsStoreError
from tests.factories import settings


def test_settings_ranges_cross_fields_and_apply_metadata() -> None:
    assert settings(log_level="debug").log_level == "DEBUG"
    assert settings().model_config["frozen"]
    assert set(Settings.model_fields) == set(Settings.FIELD_METADATA)
    with pytest.raises(ValidationError):
        settings(database_retry_initial_seconds=5, database_retry_max_seconds=1)
    with pytest.raises(ValidationError):
        settings(telegram_media_download_priority=33)
    with pytest.raises(ValidationError):
        settings(database_url="postgresql://news:secret@postgres/news")


def test_bootstrap_is_idempotent_private_and_fail_closed(tmp_path) -> None:
    first = ensure_bootstrap(tmp_path / "secrets")
    values = {path: path.read_bytes() for path in vars(first).values()}
    second = ensure_bootstrap(tmp_path / "secrets")

    assert first == second
    assert {path: path.read_bytes() for path in vars(second).values()} == values
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in vars(first).values())
    first.control_key_file.write_text("damaged")
    with pytest.raises(BootstrapError):
        load_bootstrap(tmp_path / "secrets")


def test_encrypted_revisions_rollback_and_wrong_key(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    first = store.save_draft(settings())
    second = store.save_draft(settings(log_level="DEBUG"))

    ciphertext = (tmp_path / "settings" / "revisions" / "00000001.bin").read_bytes()
    assert b"api-secret" not in ciphertext
    assert b"database-secret" not in ciphertext
    assert first == 1 and second == 2
    assert store.list_revisions()[0]["changed_fields"] == ["log_level"]

    rollback = store.rollback_to_draft(first)
    assert rollback == 3
    assert store.load_draft().log_level == "INFO"  # type: ignore[union-attr]
    assert store.manifest()["active_revision"] is None
    assert store.manifest()["checks"] is None

    other = ensure_bootstrap(tmp_path / "other")
    wrong_store = SettingsStore(tmp_path / "settings", other.settings_key_file)
    with pytest.raises(SettingsStoreError):
        wrong_store.load_revision(first)


def test_persistent_snapshot_does_not_merge_environment(monkeypatch, tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    revision = store.save_draft(settings(telegram_api_id=12345, log_level="INFO"))
    monkeypatch.setenv("TELEGRAM_API_ID", "99999")
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")

    loaded = store.load_revision(revision)
    assert loaded.telegram_api_id == 12345
    assert loaded.log_level == "INFO"


def test_checks_are_bound_to_exact_draft_and_activation(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    revision = store.save_draft(settings())
    successful = {
        name: {"ok": True, "status": "ready", "message": "ok"} for name in store.REQUIRED_CHECKS
    }
    store.save_checks(revision, successful)
    assert store.checks_passed()
    assert store.activate_draft() == revision

    store.save_draft(settings(log_level="DEBUG"))
    assert not store.checks_passed()
    with pytest.raises(SettingsStoreError):
        store.activate_draft()


def test_signed_monotonic_control_and_damaged_files(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    control = ControlChannel(tmp_path / "settings", bootstrap.control_key_file)
    assert control.request("start", 4) == 1
    assert control.request("stop") == 2
    assert control.read_request()["action"] == "stop"  # type: ignore[index]
    assert control.request_download(501) == 3
    assert control.read_request()["action"] == "download_file"  # type: ignore[index]
    assert control.read_request()["file_id"] == 501  # type: ignore[index]
    assert control.request_chat_history(-100123, 200) == 4
    history = control.read_request()
    assert history is not None
    assert history["action"] == "load_chat_history"
    assert history["chat_id"] == -100123
    assert history["from_message_id"] == 200
    assert history["limit"] == 100

    document = json.loads(control.control_path.read_text())
    document["payload"]["action"] = "restart"
    control.control_path.write_text(json.dumps(document))
    with pytest.raises(SettingsStoreError):
        control.read_request()

    control.status_path.write_text("not-json")
    with pytest.raises(SettingsStoreError):
        control.read_status()


def test_recursive_redaction() -> None:
    value = redact(
        {
            "phone_number": "+123",
            "nested": {"password": "two-factor"},
            "error": "postgresql+asyncpg://news:secret@postgres/news for +12345678901",
        }
    )
    assert value["phone_number"] == REDACTED
    assert value["nested"]["password"] == REDACTED
    assert "secret" not in value["error"]
    assert "+12345678901" not in value["error"]
